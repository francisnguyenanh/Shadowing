# -*- coding: utf-8 -*-


def _format_time(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f'{m}:{s:02d}'


CHUNK_SIZE = 300  # 5 phút mỗi lần gửi AI


# ─────────────────────────────────────────────────────────────────────────────
# Prompts KHÔNG có transcript (AI tự nghe + transcribe)
# ─────────────────────────────────────────────────────────────────────────────

def build_prompt(youtube_url: str, language: str = 'ja') -> str:
    """Build the initial AI prompt for transcribing a full YouTube video."""
    lang_label = 'tiếng Nhật' if language == 'ja' else 'tiếng Anh'
    return f"""Bạn là chuyên gia ngôn ngữ và biên dịch, chuyên tạo tài liệu học ngôn ngữ theo phương pháp shadowing.

Hãy tạo transcript cho video YouTube sau để luyện shadowing {lang_label}:
URL: {youtube_url}

Yêu cầu định dạng JSON:
{{
  "title": "tên video",
  "language": "{language}",
  "total_duration": tổng thời lượng video (giây, số thực),
  "segments": [
    {{
      "id": số thứ tự bắt đầu từ 1,
      "start": thời điểm bắt đầu (giây, số thực),
      "end": thời điểm kết thúc (giây, số thực),
      "text": "câu {lang_label} gốc",
      "translation": "bản dịch tiếng Việt"
    }}
  ]
}}

Lưu ý quan trọng:
- Mỗi segment là một câu hoặc cụm từ tự nhiên, phù hợp để luyện shadowing
- start và end phải chính xác theo video
- Bao gồm TẤT CẢ nội dung từ đầu đến cuối video
- Nếu video quá dài, dừng ở segment cuối có thể xử lý và ghi rõ id, thời điểm kết thúc
- Trả về JSON thuần túy, không có markdown code block
- Đảm bảo JSON hợp lệ (không có dấu phẩy thừa, ngoặc đúng)
- Nếu trong 1 phiên trả lời không đủ chỗ cho tất cả segments, sau khi kết thúc phiên, hãy hỏi tôi có muốn tiếp tục dịch phần còn lại không, và nếu tôi đồng ý, hãy tiếp tục với phần chưa dịch."""


def build_chunk_prompt(youtube_url: str, language: str,
                       from_time: float, to_time: float,
                       part_num: int, total_parts: int) -> str:
    """Build a prompt for a specific time window of the video (~4 min)."""
    lang_label = 'tiếng Nhật' if language == 'ja' else 'tiếng Anh'
    from_str = _format_time(from_time)
    to_str = _format_time(to_time)
    return f"""Bạn là chuyên gia ngôn ngữ và biên dịch, chuyên tạo tài liệu học ngôn ngữ theo phương pháp shadowing.

Hãy tạo transcript {lang_label} cho ĐOẠN [{from_str}–{to_str}] của video YouTube sau (Phần {part_num}/{total_parts}):
URL: {youtube_url}

⚠️ CHỈ xử lý từ giây {from_time:.1f} đến giây {to_time:.1f}. Không transcript ngoài khoảng này.

Yêu cầu định dạng JSON:
{{
  "title": "tên video",
  "language": "{language}",
  "segments": [
    {{
      "id": 1,
      "start": thời điểm bắt đầu (giây, trong khoảng {from_time:.1f}–{to_time:.1f}),
      "end": thời điểm kết thúc (giây),
      "text": "câu {lang_label} gốc",
      "translation": "bản dịch tiếng Việt"
    }}
  ]
}}

Lưu ý:
- Mỗi segment là một câu hoặc cụm từ tự nhiên, phù hợp để luyện shadowing
- start và end phải chính xác trong khoảng {from_str}–{to_str}
- id bắt đầu từ 1 (app tự đánh số lại khi gộp các phần)
- Trả về JSON thuần túy, không có markdown code block
- Đảm bảo JSON hợp lệ (không dấu phẩy thừa, ngoặc đúng)
- Nếu trong 1 phiên trả lời không đủ chỗ cho tất cả segments, sau khi kết thúc phiên, hãy hỏi tôi có muốn tiếp tục dịch phần còn lại không, và nếu tôi đồng ý, hãy tiếp tục với phần chưa dịch."""


# ─────────────────────────────────────────────────────────────────────────────
# Prompts CÓ transcript gốc từ YouTube (AI chỉ cần dịch + format lại)
# ─────────────────────────────────────────────────────────────────────────────

def build_chunk_prompt_with_transcript(youtube_url: str, language: str,
                                       from_time: float, to_time: float,
                                       part_num: int, total_parts: int,
                                       transcript_segments: list) -> str:
    """Build a prompt that includes raw YouTube transcript so AI only adds translations.

    transcript_segments: list of {"start": float, "duration": float, "text": str}
    """
    lang_label = 'tiếng Nhật' if language == 'ja' else 'tiếng Anh'
    from_str = _format_time(from_time)
    to_str = _format_time(to_time)

    chunk_segs = [
        s for s in transcript_segments
        if s['start'] >= from_time and s['start'] < to_time
    ]

    if not chunk_segs:
        return build_chunk_prompt(youtube_url, language, from_time, to_time, part_num, total_parts)

    transcript_block = '\n'.join(
        f"[{_format_time(s['start'])}→{_format_time(s['start'] + s.get('duration', 2.0))}] {s['text']}"
        for s in chunk_segs
    )

    return f"""Bạn là chuyên gia biên dịch {lang_label}–Việt, tạo tài liệu học ngôn ngữ theo phương pháp shadowing.

Video YouTube: {youtube_url}
Đoạn: [{from_str}–{to_str}] (Phần {part_num}/{total_parts})

📝 TRANSCRIPT GỐC TỪ YOUTUBE (ĐÃ CÓ SẴN — KHÔNG cần tự nghe lại):
{transcript_block}

MỤC ĐÍCH: Phân tách câu hoàn chỉnh để người dùng luyện shadowing {lang_label}
NHIỆM VỤ: 
1. Hãy phân tách câu nội dung của câu {lang_label} trong các segment sau cho hoàn chỉnh không bị ngắt mạch câu, update lại timeline nếu có điều chỉnh lại câu {lang_label}
2. Thêm bản dịch tiếng Việt cho từng dòng. 
3. KHÔNG thêm hay bỏ bớt nội dung câu gốc {lang_label}.

Yêu cầu định dạng JSON:
{{
  "title": "tên video",
  "language": "{language}",
  "segments": [
    {{
      "id": 1,
      "start": thời điểm bắt đầu (giây, trong khoảng {from_time:.1f}–{to_time:.1f}),
      "end": thời điểm kết thúc (giây),
      "text": "câu {lang_label} gốc (lấy từ transcript bên trên)",
      "translation": "bản dịch tiếng Việt"
    }}
  ]
}}

Lưu ý:
- id bắt đầu từ 1 (app tự đánh số lại khi gộp)
- Trả về JSON thuần túy, không có markdown code block
- Đảm bảo JSON hợp lệ (không dấu phẩy thừa, ngoặc đúng)
- Nếu trong 1 phiên trả lời không đủ chỗ cho tất cả segments, sau khi kết thúc phiên, hãy hỏi tôi có muốn tiếp tục dịch phần còn lại không, và nếu tôi đồng ý, hãy tiếp tục với phần chưa dịch."""


# ─────────────────────────────────────────────────────────────────────────────
# Builder chính
# ─────────────────────────────────────────────────────────────────────────────

def build_chunked_prompts(youtube_url: str, language: str,
                          duration: int, chunk_size: int = CHUNK_SIZE,
                          transcript_data: list = None) -> list:
    """Split video into chunks and build one prompt per window.

    transcript_data: optional list of {"start", "duration", "text"} from YouTube.
    If provided, each chunk prompt embeds the relevant raw transcript.
    """
    if not duration or duration <= 0:
        if transcript_data:
            prompt = build_chunk_prompt_with_transcript(
                youtube_url, language, 0, 0, 1, 1, transcript_data
            )
        else:
            prompt = build_prompt(youtube_url, language)
        return [{'part': 1, 'total_parts': 1,
                 'from_time': 0, 'to_time': 0,
                 'from_str': '0:00', 'to_str': '?',
                 'prompt': prompt,
                 'has_transcript': bool(transcript_data)}]

    total_parts = (duration + chunk_size - 1) // chunk_size
    chunks = []
    t = 0
    part_num = 1
    while t < duration:
        end_t = min(t + chunk_size, duration)
        if transcript_data:
            prompt = build_chunk_prompt_with_transcript(
                youtube_url, language, t, end_t, part_num, total_parts, transcript_data
            )
        else:
            prompt = build_chunk_prompt(youtube_url, language, t, end_t, part_num, total_parts)
        chunks.append({'part': part_num, 'total_parts': total_parts,
                       'from_time': t, 'to_time': end_t,
                       'from_str': _format_time(t), 'to_str': _format_time(end_t),
                       'prompt': prompt, 'has_transcript': bool(transcript_data)})
        t = end_t
        part_num += 1
    return chunks


def build_continuation_prompt(youtube_url: str, language: str,
                               last_id: int, last_end_time: float) -> str:
    """Build a continuation prompt to get the next batch of segments."""
    lang_label = 'tiếng Nhật' if language == 'ja' else 'tiếng Anh'
    minutes = int(last_end_time // 60)
    seconds = last_end_time % 60
    time_str = f'{minutes}:{seconds:05.2f}'
    return f"""Tiếp tục tạo transcript cho video YouTube sau để luyện shadowing {lang_label}:
URL: {youtube_url}

Transcript đã được tạo đến segment id={last_id}, end_time={last_end_time}s ({time_str}).
Hãy tiếp tục từ thời điểm {last_end_time}s đến hết video.

Yêu cầu định dạng JSON (CHỈ trả về segments mới, bắt đầu từ id={last_id + 1}):
{{
  "title": "tên video (giữ nguyên)",
  "language": "{language}",
  "total_duration": tổng thời lượng video (giây, số thực),
  "segments": [
    {{
      "id": {last_id + 1},
      "start": {last_end_time},
      "end": ...,
      "text": "câu {lang_label} gốc",
      "translation": "bản dịch tiếng Việt"
    }},
    ...
  ]
}}

Lưu ý:
- id bắt đầu từ {last_id + 1}
- start_time của segment đầu tiên là {last_end_time}
- Bao gồm tất cả nội dung còn lại đến hết video
- Trả về JSON thuần túy, không có markdown code block"""


def build_srt_translation_prompt(language: str, title: str = '', segment_count: int = 0) -> str:
    """Build a simple translation prompt for when user attaches the JSON transcript file to AI."""
    lang_label = 'tiếng Nhật' if language == 'ja' else 'tiếng Anh'
    count_note = f' ({segment_count} segments)' if segment_count else ''
    title_note = f' "{title}"' if title else ''
    return f"""Bạn là chuyên gia xử lý transcript  {lang_label} cho mục đích luyện shadowing.

## DỮ LIỆU ĐẦU VÀO
File JSON đính kèm chứa transcript {lang_label} {title} với [{segment_count}] segments, mỗi segment có: id, start, end, text, translation.

## NHIỆM VỤ

### Bước 1 – Ghép câu hoàn chỉnh (sentence merging)
Nhiều segment liên tiếp thường chứa một câu bị cắt vụn do auto-transcribe. Hãy ghép các segment liên tiếp lại thành một câu hoàn chỉnh theo nguyên tắc:
- Câu kết thúc khi gặp dấu: 。！？ hoặc khoảng dừng ngữ nghĩa rõ ràng.
- Câu KHÔNG kết thúc ở giữa trạng từ nối (けど、から、て、で、が nối câu...).
- `start` = start của segment đầu tiên được ghép, `end` = end của segment cuối cùng được ghép.
- `id` giữ nguyên id của segment đầu tiên trong nhóm ghép.
- Nếu 1 segment đã là câu hoàn chỉnh, giữ nguyên.

Ví dụ:
Input:
{{"id":3,"start":6.4,"end":8.3,"text":"ます。ま、そもそもいきなり話がうまくな"}}
{{"id":4,"start":8.3,"end":10.6,"text":"るってのはほぼ不可能でして、話がうまく"}}
{{"id":5,"start":10.6,"end":13.0,"text":"なるためにはですね、日常生活から最初は"}}

Output sau ghép (câu chưa kết thúc nên tiếp tục ghép):
→ Tiếp tục đến khi gặp dấu câu hoặc ngừng ngữ nghĩa.

### Bước 2 – Thêm bản dịch tiếng Việt
- Điền vào trường `"translation"` của mỗi segment SAU KHI đã ghép.
- Dịch sát nghĩa, ngắn gọn, giữ nhịp tương đương câu {lang_label} (phù hợp shadowing).
- KHÔNG dịch thoáng hoặc diễn giải dài.
- Giữ nguyên 100% nội dung `text` {lang_label}, không sửa, không bổ sung.

Ví dụ dịch đúng phong cách:
JP: 話がうまくなるためには、日常生活から練習する必要があります。
VI: Để nói chuyện giỏi hơn, bạn cần luyện tập từ trong cuộc sống hàng ngày.

## FORMAT ĐẦU RA
- JSON thuần túy, KHÔNG có markdown (không có ```json).
- Cùng cấu trúc gốc: {{"title":..., "language":..., "segments":[...]}}.
- JSON hợp lệ: không dấu phẩy thừa, ngoặc đóng đúng.

## XỬ LÝ BATCH
Vì có nhiều segments, hãy xử lý theo batch: segments [1–150] trước.
Sau khi hoàn thành, thông báo: "✅ Hoàn thành batch 1 (segments 1–150). Gửi 'tiếp tục' để nhận batch 2 (151–300)."
Không tự động tiếp tục sang batch tiếp theo."""
