# -*- coding: utf-8 -*-
import json
import re


def parse_transcript(raw_text: str) -> dict:
    """Parse a single raw AI response into a transcript dict."""
    if not raw_text or not raw_text.strip():
        raise ValueError("Nội dung JSON trống. Vui lòng paste kết quả từ AI.")

    text = raw_text.strip()

    # Strip markdown fences if present
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text)
    text = text.strip()

    # Extract JSON substring between first { and last }
    start_idx = text.find('{')
    end_idx = text.rfind('}')
    if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
        raise ValueError("Không tìm thấy JSON hợp lệ. Hãy chắc chắn output bắt đầu bằng { và kết thúc bằng }.")

    json_str = text[start_idx:end_idx + 1]

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON không hợp lệ: {str(e)}. Hãy kiểm tra lại output từ AI.")

    # Validate required top-level keys
    required_keys = ['title', 'language', 'segments']
    for key in required_keys:
        if key not in data:
            raise ValueError(f"Thiếu trường bắt buộc '{key}' trong JSON.")

    if not isinstance(data['segments'], list) or len(data['segments']) == 0:
        raise ValueError("Trường 'segments' phải là một mảng không rỗng.")

    # Validate each segment
    for i, seg in enumerate(data['segments']):
        for field in ['id', 'start', 'end', 'text']:
            if field not in seg:
                raise ValueError(f"Segment #{i + 1} thiếu trường bắt buộc '{field}'.")
        try:
            float(seg['start'])
            float(seg['end'])
        except (TypeError, ValueError):
            raise ValueError(f"Segment #{i + 1}: 'start' và 'end' phải là số.")

    return data


def save_transcript(video_db_id: int, parsed_dict: dict, db) -> None:
    # Delete existing segments for this video first (idempotent)
    db.execute('DELETE FROM segments WHERE video_id = ?', (video_db_id,))

    segments = parsed_dict.get('segments', [])
    for seg in segments:
        db.execute(
            '''INSERT INTO segments
               (video_id, segment_order, start_time, end_time, text, translation)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (
                video_db_id,
                int(seg.get('id', 0)),
                float(seg['start']),
                float(seg['end']),
                str(seg.get('text', '')),
                seg.get('translation') or None,
            )
        )

    # Update title and duration if provided
    title = parsed_dict.get('title')
    duration = parsed_dict.get('duration')
    if title or duration:
        db.execute(
            'UPDATE videos SET title = COALESCE(?, title), duration = COALESCE(?, duration) WHERE id = ?',
            (title, duration, video_db_id)
        )

    db.commit()


def parse_and_merge_transcripts(raw_texts: list) -> dict:
    """Parse multiple JSON blocks from separate AI sessions and merge them into one transcript.

    Segments are sorted by start_time and re-numbered sequentially.
    Duplicate segments (same start+end+text) are de-duped automatically.
    """
    if not raw_texts:
        raise ValueError("Không có dữ liệu JSON nào được cung cấp.")

    all_segments = []
    base_info = None
    parsed_count = 0

    for i, raw_text in enumerate(raw_texts):
        if not raw_text or not raw_text.strip():
            continue
        try:
            data = parse_transcript(raw_text)
        except ValueError as e:
            raise ValueError(f"Phần JSON #{i + 1} lỗi: {e}")

        if base_info is None:
            base_info = data

        all_segments.extend(data.get('segments', []))
        parsed_count += 1

    if base_info is None or parsed_count == 0:
        raise ValueError("Tất cả các phần JSON đều trống hoặc không hợp lệ.")

    if not all_segments:
        raise ValueError("Không tìm thấy segment nào trong các JSON đã cung cấp.")

    # Sort by start time
    all_segments.sort(key=lambda s: float(s.get('start', 0)))

    # De-duplicate: remove segments with exact same (start, end, text)
    seen = set()
    unique_segments = []
    for seg in all_segments:
        key = (round(float(seg.get('start', 0)), 3), round(float(seg.get('end', 0)), 3), seg.get('text', '').strip())
        if key not in seen:
            seen.add(key)
            unique_segments.append(seg)

    # Re-number IDs sequentially
    for idx, seg in enumerate(unique_segments, start=1):
        seg['id'] = idx

    result = dict(base_info)
    result['segments'] = unique_segments
    return result


def apply_timeline_offset(video_db_id: int, offset: float, db) -> int:
    """Apply a time offset (seconds) to all segments of a video. Returns number of rows updated."""
    cursor = db.execute(
        'UPDATE segments SET start_time = MAX(0, start_time + ?), end_time = MAX(0, end_time + ?) WHERE video_id = ?',
        (offset, offset, video_db_id)
    )
    db.commit()
    return cursor.rowcount


def parse_srt(text: str) -> list:
    """Parse SRT subtitle text into a list of {"start", "duration", "text"} dicts.

    Handles standard SRT and YouTube-style SRT with embedded timing/color tags
    (e.g. <c.white>, <00:00:00.480>, <c.color...>).
    Returns list of {"start": float, "duration": float, "text": str}.
    Raises ValueError if no valid subtitles found.
    """
    def to_secs(ts: str) -> float:
        ts = ts.strip().replace(',', '.')
        parts = ts.split(':')
        h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
        return h * 3600 + m * 60 + s

    # Normalize line endings
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    segments = []
    blocks = re.split(r'\n{2,}', text.strip())

    for block in blocks:
        lines = [l for l in block.strip().splitlines() if l.strip()]
        if len(lines) < 2:
            continue

        # Skip sequence number line if present
        start_line = 0
        if re.match(r'^\d+$', lines[0].strip()):
            start_line = 1
        if start_line >= len(lines):
            continue

        # Parse timestamp line
        ts_match = re.match(
            r'(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})',
            lines[start_line].strip()
        )
        if not ts_match:
            continue

        start = to_secs(ts_match.group(1))
        end = to_secs(ts_match.group(2))

        text_lines = lines[start_line + 1:]
        if not text_lines:
            continue

        seg_text = ' '.join(line.strip() for line in text_lines)
        # Strip HTML/timing tags (e.g. <c.white>, <00:00:01.260>, </c>)
        seg_text = re.sub(r'<[^>]+>', '', seg_text)
        seg_text = re.sub(r'\s+', ' ', seg_text).strip()

        if seg_text:
            segments.append({
                'start': start,
                'duration': max(0.1, end - start),
                'text': seg_text,
            })

    if not segments:
        raise ValueError("Không tìm thấy subtitle nào trong file .srt. Hãy kiểm tra lại file.")
    return segments
