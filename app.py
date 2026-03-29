# -*- coding: utf-8 -*-
import re
import json
from flask import Flask, render_template, request, redirect, url_for, flash, g, jsonify
import config
from database import get_db, init_db, close_db
from prompt_builder import build_prompt, build_continuation_prompt, build_chunked_prompts, build_chunk_prompt_with_transcript, build_srt_translation_prompt
from transcript_parser import parse_transcript, parse_and_merge_transcripts, save_transcript, apply_timeline_offset, parse_srt

app = Flask(__name__)
app.config.from_object(config)
app.secret_key = config.SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = config.MAX_CONTENT_LENGTH


# ── DB lifecycle ──────────────────────────────────────────────────────────────

@app.teardown_appcontext
def teardown_db(exception):
    close_db(exception)


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from all common URL formats."""
    patterns = [
        r'(?:youtube\.com/watch\?.*v=)([A-Za-z0-9_-]{11})',
        r'(?:youtu\.be/)([A-Za-z0-9_-]{11})',
        r'(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})',
        r'(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def fetch_video_info(youtube_url: str) -> dict:
    """Try yt-dlp to get title and duration. Falls back gracefully."""
    try:
        import yt_dlp
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
            return {
                'title': info.get('title', youtube_url),
                'duration': int(info.get('duration', 0) or 0),
            }
    except Exception:
        return {'title': youtube_url, 'duration': 0}


def download_audio_task(youtube_url: str, video_id: str) -> str:
    """Download audio from YouTube as MP3 192kbps.

    Output filename: static/audio/<Sanitized Title> [<video_id>].mp3
    Returns the path relative to static/ (e.g. 'audio/Title_[ID].mp3') so it can
    be stored in the DB and served via url_for('static', filename=...).
    Raises on failure so the caller can flash the error.
    """
    import os
    import yt_dlp

    audio_dir = os.path.join(app.static_folder, 'audio')
    os.makedirs(audio_dir, exist_ok=True)

    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'outtmpl': os.path.join(audio_dir, '%(title)s [%(id)s].%(ext)s'),
        'restrictfilenames': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=True)
        # prepare_filename reflects the same sanitization as restrictfilenames;
        # the postprocessor swaps the container ext to .mp3.
        raw_path = ydl.prepare_filename(info)
        mp3_path = os.path.splitext(raw_path)[0] + '.mp3'

    # Return path relative to static/ for url_for('static', filename=...)
    return 'audio/' + os.path.basename(mp3_path)


def fetch_youtube_transcript(video_id: str, youtube_url: str, language: str) -> list | None:
    """Fetch transcript from YouTube. Try youtube-transcript-api first, then yt-dlp subtitles.

    Returns list of {"start": float, "duration": float, "text": str} or None.
    """
    # Approach 1: youtube-transcript-api
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        lang_codes = (['ja', 'ja-orig'] if language == 'ja' else ['en', 'en-orig'])
        try:
            segs = YouTubeTranscriptApi.get_transcript(video_id, languages=lang_codes)
        except Exception:
            segs = YouTubeTranscriptApi.get_transcript(video_id)  # any available language
        return [{'start': s['start'], 'duration': s['duration'], 'text': s['text']} for s in segs]
    except Exception:
        pass

    # Approach 3 (fallback): yt-dlp subtitle download to temp dir
    try:
        import yt_dlp
        import os
        import tempfile
        lang_codes = (['ja', 'en'] if language == 'ja' else ['en', 'ja'])
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = {
                'skip_download': True,
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': lang_codes,
                'subtitlesformat': 'json3',
                'outtmpl': os.path.join(tmpdir, '%(id)s'),
                'quiet': True,
                'no_warnings': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([youtube_url])
            for fname in sorted(os.listdir(tmpdir)):
                if fname.endswith('.json3'):
                    fpath = os.path.join(tmpdir, fname)
                    with open(fpath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    segments = []
                    for event in data.get('events', []):
                        start_ms = event.get('tStartMs', 0)
                        dur_ms = event.get('dDurationMs', 2000)
                        text = ''.join(s.get('utf8', '') for s in event.get('segs', []))
                        text = text.strip()
                        if text and text != '\n':
                            segments.append({
                                'start': start_ms / 1000,
                                'duration': dur_ms / 1000,
                                'text': text,
                            })
                    if segments:
                        return segments
    except Exception:
        pass

    return None

@app.route('/')
def index():
    try:
        db = get_db()
        playlist_id = request.args.get('playlist', type=int)
        playlists = db.execute(
            '''SELECT p.*, COUNT(pv.id) as video_count
               FROM playlists p
               LEFT JOIN playlist_videos pv ON pv.playlist_id = p.id
               GROUP BY p.id
               ORDER BY p.name COLLATE NOCASE'''
        ).fetchall()
        if playlist_id:
            current_playlist = db.execute(
                'SELECT * FROM playlists WHERE id = ?', (playlist_id,)
            ).fetchone()
            if current_playlist is None:
                return redirect(url_for('index'))
            videos = db.execute(
                '''SELECT v.*, COUNT(s.id) as segment_count
                   FROM videos v
                   JOIN playlist_videos pv ON pv.video_id = v.id
                   LEFT JOIN segments s ON s.video_id = v.id
                   WHERE pv.playlist_id = ?
                   GROUP BY v.id
                   ORDER BY pv.added_at DESC''',
                (playlist_id,)
            ).fetchall()
        else:
            current_playlist = None
            videos = db.execute(
                '''SELECT v.*, COUNT(s.id) as segment_count
                   FROM videos v
                   LEFT JOIN segments s ON s.video_id = v.id
                   GROUP BY v.id
                   ORDER BY v.created_at DESC'''
            ).fetchall()
    except Exception as e:
        flash(f'Lỗi khi tải danh sách video: {str(e)}', 'error')
        videos = []
        playlists = []
        current_playlist = None
        playlist_id = None
    return render_template('index.html', videos=videos, playlists=playlists,
                           current_playlist=current_playlist, playlist_id=playlist_id)


@app.route('/add', methods=['GET'])
def add_video_form():
    return render_template('add_video.html')


@app.route('/add', methods=['POST'])
def add_video():
    youtube_url = request.form.get('youtube_url', '').strip()
    language = request.form.get('language', 'ja').strip()

    if not youtube_url:
        flash('Vui lòng nhập URL YouTube.', 'error')
        return render_template('add_video.html')

    video_id = extract_video_id(youtube_url)
    if not video_id:
        flash('URL YouTube không hợp lệ. Hãy kiểm tra lại định dạng URL.', 'error')
        return render_template('add_video.html')

    if language not in ('ja', 'en'):
        language = 'ja'

    try:
        info = fetch_video_info(youtube_url)
        db = get_db()
        cursor = db.execute(
            'INSERT INTO videos (youtube_url, video_id, title, language, duration) VALUES (?, ?, ?, ?, ?)',
            (youtube_url, video_id, info['title'], language, info['duration'])
        )
        db.commit()
        video_db_id = cursor.lastrowid
        # Import SRT file if provided
        srt_file = request.files.get('srt_file')
        if srt_file and srt_file.filename:
            try:
                srt_text = srt_file.read().decode('utf-8', errors='replace')
                transcript_data = parse_srt(srt_text)
                db.execute('UPDATE videos SET transcript_raw = ? WHERE id = ?',
                           (json.dumps(transcript_data, ensure_ascii=False), video_db_id))
                db.commit()
                flash(f'Đã import {len(transcript_data)} dòng từ file SRT.', 'success')
            except Exception as srt_err:
                flash(f'Lỗi khi đọc file SRT: {str(srt_err)}', 'error')
        # Download audio (MP3) — non-blocking on error
        """
        try:
            audio_file = download_audio_task(youtube_url, video_id)
            db.execute('UPDATE videos SET audio_path = ? WHERE id = ?', (audio_file, video_db_id))
            db.commit()
            flash('Video đã được thêm và audio đã tải xong!', 'success')
        except Exception as dl_err:
            flash('Video đã thêm nhưng tải audio thất bại: ' + str(dl_err), 'error')
        """
        return redirect(url_for('show_prompt', video_db_id=video_db_id))
    except Exception as e:
        flash(f'Lỗi khi thêm video: {str(e)}', 'error')
        return render_template('add_video.html')


@app.route('/prompt/<int:video_db_id>')
def show_prompt(video_db_id):
    try:
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            flash('Không tìm thấy video.', 'error')
            return redirect(url_for('index'))
        transcript_data = json.loads(video['transcript_raw']) if video['transcript_raw'] else None
        chunks = build_chunked_prompts(
            video['youtube_url'], video['language'], video['duration'] or 0,
            transcript_data=transcript_data
        )
        transcript_count = len(transcript_data) if transcript_data else 0
        srt_prompt = build_srt_translation_prompt(
            video['language'], video['title'] or '', transcript_count
        ) if transcript_count > 0 else ''
        return render_template('paste_transcript.html', video=video, chunks=chunks,
                               transcript_count=transcript_count,
                               srt_translation_prompt=srt_prompt, error=None)
    except Exception as e:
        flash(f'Lỗi: {str(e)}', 'error')
        return redirect(url_for('index'))


@app.route('/api/continuation_prompt/<int:video_db_id>', methods=['POST'])
def api_continuation_prompt(video_db_id):
    """Return a continuation prompt given last segment ID and end time."""
    try:
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            return jsonify({'error': 'Không tìm thấy video.'}), 404
        data = request.get_json(silent=True) or {}
        last_id = int(data.get('last_id', 0))
        last_end = float(data.get('last_end', 0.0))
        prompt_text = build_continuation_prompt(video['youtube_url'], video['language'], last_id, last_end)
        return jsonify({'prompt': prompt_text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/transcript/<int:video_db_id>', methods=['POST'])
def save_transcript_route(video_db_id):
    db = get_db()
    video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
    if video is None:
        flash('Không tìm thấy video.', 'error')
        return redirect(url_for('index'))

    # Support multiple JSON parts submitted as repeated fields
    raw_jsons = [t for t in request.form.getlist('transcript_json') if t.strip()]
    transcript_data = json.loads(video['transcript_raw']) if video['transcript_raw'] else None
    chunks = build_chunked_prompts(video['youtube_url'], video['language'], video['duration'] or 0,
                                   transcript_data=transcript_data)
    transcript_count = len(transcript_data) if transcript_data else 0
    srt_prompt = build_srt_translation_prompt(
        video['language'], video['title'] or '', transcript_count
    ) if transcript_count > 0 else ''

    if not raw_jsons:
        return render_template('paste_transcript.html', video=video, chunks=chunks,
                               transcript_count=transcript_count,
                               srt_translation_prompt=srt_prompt,
                               error='Vui lòng nhập ít nhất một đoạn JSON.')
    try:
        parsed = parse_and_merge_transcripts(raw_jsons)
        save_transcript(video_db_id, parsed, db)
        seg_count = len(parsed.get('segments', []))
        flash(f'Đã lưu {seg_count} segments thành công! ({len(raw_jsons)} phần JSON)', 'success')
        return redirect(url_for('player', video_db_id=video_db_id))
    except ValueError as e:
        return render_template(
            'paste_transcript.html',
            video=video,
            chunks=chunks,
            transcript_count=transcript_count,
            srt_translation_prompt=srt_prompt,
            error=str(e),
            previous_inputs=raw_jsons
        )
    except Exception as e:
        return render_template(
            'paste_transcript.html',
            video=video,
            chunks=chunks,
            transcript_count=transcript_count,
            srt_translation_prompt=srt_prompt,
            error=f'Lỗi không xác định: {str(e)}',
            previous_inputs=raw_jsons
        )


@app.route('/player/<int:video_db_id>')
def player(video_db_id):
    try:
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            flash('Không tìm thấy video.', 'error')
            return redirect(url_for('index'))
        segments = db.execute(
            'SELECT * FROM segments WHERE video_id = ? ORDER BY segment_order',
            (video_db_id,)
        ).fetchall()
        segments_list = [dict(s) for s in segments]
        return render_template('player.html', video=video, segments=segments_list,
                               segments_json=json.dumps(segments_list))
    except Exception as e:
        flash(f'Lỗi khi tải player: {str(e)}', 'error')
        return redirect(url_for('index'))


@app.route('/api/segments/<int:video_db_id>')
def api_segments(video_db_id):
    try:
        db = get_db()
        segments = db.execute(
            'SELECT id, start_time as start, end_time as end, text, translation, bookmarked '
            'FROM segments WHERE video_id = ? ORDER BY segment_order',
            (video_db_id,)
        ).fetchall()
        return jsonify([dict(s) for s in segments])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/segment/<int:segment_id>', methods=['PATCH'])
def api_update_segment_time(segment_id):
    """Update segment timestamps. Supports cascade on start_time change:
    - prev segment's end_time becomes new start_time
    - all subsequent segments shift by the same delta
    """
    try:
        data = request.get_json(silent=True) or {}
        db = get_db()
        seg = db.execute('SELECT * FROM segments WHERE id = ?', (segment_id,)).fetchone()
        if seg is None:
            return jsonify({'success': False, 'error': 'Không tìm thấy segment.'}), 404

        cascade = bool(data.get('cascade', False))
        has_start = 'start_time' in data
        has_end = 'end_time' in data
        updated = []

        # Handle text content fields (text, translation, bookmarked)
        content_fields = {}
        for field in ('text', 'translation'):
            if field in data:
                content_fields[field] = str(data[field])
        if 'bookmarked' in data:
            content_fields['bookmarked'] = int(bool(data['bookmarked']))
        if content_fields:
            set_clause = ', '.join(f'{k} = ?' for k in content_fields)
            db.execute(f'UPDATE segments SET {set_clause} WHERE id = ?',
                       (*content_fields.values(), segment_id))

        if has_start:
            new_start = float(data['start_time'])
            old_start = seg['start_time']
            old_end = seg['end_time']
            # Preserve duration when only start is provided
            new_end = float(data['end_time']) if has_end else old_end + (new_start - old_start)
            if new_start < 0 or new_end <= new_start:
                return jsonify({'success': False, 'error': 'Timestamp không hợp lệ (start >= 0, start < end).'}), 400

            delta = new_start - old_start

            # Update previous segment's end → new start (snap gap closed)
            if cascade and abs(delta) > 0.001:
                prev = db.execute(
                    'SELECT * FROM segments WHERE video_id = ? AND segment_order < ? '
                    'ORDER BY segment_order DESC LIMIT 1',
                    (seg['video_id'], seg['segment_order'])
                ).fetchone()
                if prev:
                    db.execute('UPDATE segments SET end_time = ? WHERE id = ?',
                               (new_start, prev['id']))
                    updated.append({'id': prev['id'], 'start_time': prev['start_time'],
                                    'end_time': new_start})

            # Update this segment
            db.execute('UPDATE segments SET start_time = ?, end_time = ? WHERE id = ?',
                       (new_start, new_end, segment_id))
            updated.append({'id': segment_id, 'start_time': new_start, 'end_time': new_end})

            # Shift all subsequent segments by delta
            if cascade and abs(delta) > 0.001:
                subsequents = db.execute(
                    'SELECT * FROM segments WHERE video_id = ? AND segment_order > ? '
                    'ORDER BY segment_order',
                    (seg['video_id'], seg['segment_order'])
                ).fetchall()
                for s in subsequents:
                    ns = max(0.0, round(s['start_time'] + delta, 3))
                    ne = max(0.0, round(s['end_time'] + delta, 3))
                    db.execute('UPDATE segments SET start_time = ?, end_time = ? WHERE id = ?',
                               (ns, ne, s['id']))
                    updated.append({'id': s['id'], 'start_time': ns, 'end_time': ne})

        elif has_end:
            new_end = float(data['end_time'])
            if new_end <= seg['start_time']:
                return jsonify({'success': False, 'error': 'end_time phải > start_time.'}), 400
            db.execute('UPDATE segments SET end_time = ? WHERE id = ?', (new_end, segment_id))
            updated.append({'id': segment_id, 'start_time': seg['start_time'], 'end_time': new_end})
        else:
            if not content_fields:
                return jsonify({'success': False, 'error': 'Thiếu trường cần cập nhật.'}), 400

        db.commit()
        return jsonify({'success': True, 'cascade': cascade, 'updated': updated})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/video/<int:video_db_id>', methods=['DELETE'])
def api_delete_video(video_db_id):
    try:
        db = get_db()
        db.execute('DELETE FROM segments WHERE video_id = ?', (video_db_id,))
        db.execute('DELETE FROM playlist_videos WHERE video_id = ?', (video_db_id,))
        db.execute('DELETE FROM videos WHERE id = ?', (video_db_id,))
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/transcript_json/<int:video_db_id>')
def api_download_transcript_json(video_db_id):
    """Download transcript as JSON with empty translations — for attaching to external AI."""
    from flask import Response
    try:
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            return jsonify({'error': 'Không tìm thấy video.'}), 404
        if not video['transcript_raw']:
            return jsonify({'error': 'Video này chưa có transcript.'}), 404

        transcript_data = json.loads(video['transcript_raw'])
        segments = []
        for i, seg in enumerate(transcript_data, start=1):
            segments.append({
                'id': i,
                'start': seg['start'],
                'end': round(seg['start'] + seg.get('duration', 2.0), 3),
                'text': seg['text'],
                'translation': None,
            })

        output = {
            'title': video['title'] or video['video_id'],
            'language': video['language'],
            'segments': segments,
        }
        content = json.dumps(output, ensure_ascii=False, indent=2)
        
        import unicodedata
        title_safe = re.sub(r'[^\w\s-]', '', video['title'] or video['video_id'])[:50].strip()
        # Loại bỏ Unicode, chỉ giữ ASCII
        title_ascii = unicodedata.normalize('NFKD', title_safe).encode('ascii', 'ignore').decode('ascii')
        filename = f'transcript_{title_ascii}_{video["video_id"]}.json'
        
        return Response(
            content,
            mimetype='application/json; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload_srt/<int:video_db_id>', methods=['POST'])
def api_upload_srt(video_db_id):
    """Upload or replace SRT transcript for an existing video."""
    db = get_db()
    video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
    if video is None:
        flash('Không tìm thấy video.', 'error')
        return redirect(url_for('index'))

    srt_file = request.files.get('srt_file')
    if not srt_file or not srt_file.filename:
        flash('Vui lòng chọn file .srt.', 'error')
        return redirect(url_for('show_prompt', video_db_id=video_db_id))

    try:
        srt_text = srt_file.read().decode('utf-8', errors='replace')
        transcript_data = parse_srt(srt_text)
        db.execute('UPDATE videos SET transcript_raw = ? WHERE id = ?',
                   (json.dumps(transcript_data, ensure_ascii=False), video_db_id))
        db.commit()
        flash(f'Đã import {len(transcript_data)} dòng transcript từ file SRT.', 'success')
    except Exception as e:
        flash(f'Lỗi khi đọc file SRT: {str(e)}', 'error')

    return redirect(url_for('show_prompt', video_db_id=video_db_id))


@app.route('/api/transcript_file/<int:video_db_id>')
def api_download_transcript_file(video_db_id):
    """Download raw YouTube transcript as a Markdown file for attaching to external AI."""
    try:
        from flask import Response
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            return jsonify({'error': 'Không tìm thấy video.'}), 404
        if not video['transcript_raw']:
            return jsonify({'error': 'Video này không có transcript từ YouTube.'}), 404

        transcript_data = json.loads(video['transcript_raw'])
        lines = [
            f'# Transcript: {video["title"] or video["video_id"]}',
            f'URL: {video["youtube_url"]}',
            f'Language: {video["language"]}',
            '',
            '## Segments',
            '',
        ]
        for seg in transcript_data:
            start = seg['start']
            end = start + seg.get('duration', 2.0)
            m_s, s_s = int(start // 60), int(start % 60)
            m_e, s_e = int(end // 60), int(end % 60)
            lines.append(f'[{m_s:02d}:{s_s:02d} → {m_e:02d}:{s_e:02d}] {seg["text"]}')

        content = '\n'.join(lines)
        title_safe = re.sub(r'[^\w\s-]', '', video['title'] or video['video_id'])[:50].strip()
        filename = f'transcript_{title_safe}_{video["video_id"]}.md'
        return Response(
            content,
            mimetype='text/markdown; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Playlist API ──────────────────────────────────────────────────────────────

@app.route('/api/playlist', methods=['POST'])
def api_create_playlist():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Tên playlist không được để trống.'}), 400
    try:
        db = get_db()
        cursor = db.execute('INSERT INTO playlists (name) VALUES (?)', (name,))
        db.commit()
        return jsonify({'success': True, 'id': cursor.lastrowid, 'name': name})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/playlist/<int:playlist_id>', methods=['DELETE'])
def api_delete_playlist(playlist_id):
    try:
        db = get_db()
        db.execute('DELETE FROM playlist_videos WHERE playlist_id = ?', (playlist_id,))
        db.execute('DELETE FROM playlists WHERE id = ?', (playlist_id,))
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/playlist/<int:playlist_id>/video', methods=['POST'])
def api_playlist_add_video(playlist_id):
    data = request.get_json(silent=True) or {}
    video_id = data.get('video_id')
    if not video_id:
        return jsonify({'success': False, 'error': 'Thiếu video_id.'}), 400
    try:
        db = get_db()
        # Verify both records exist
        if not db.execute('SELECT 1 FROM playlists WHERE id = ?', (playlist_id,)).fetchone():
            return jsonify({'success': False, 'error': 'Playlist không tồn tại.'}), 404
        if not db.execute('SELECT 1 FROM videos WHERE id = ?', (video_id,)).fetchone():
            return jsonify({'success': False, 'error': 'Video không tồn tại.'}), 404
        db.execute(
            'INSERT OR IGNORE INTO playlist_videos (playlist_id, video_id) VALUES (?, ?)',
            (playlist_id, video_id)
        )
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/playlist/<int:playlist_id>/video/<int:video_id>', methods=['DELETE'])
def api_playlist_remove_video(playlist_id, video_id):
    try:
        db = get_db()
        db.execute(
            'DELETE FROM playlist_videos WHERE playlist_id = ? AND video_id = ?',
            (playlist_id, video_id)
        )
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/video/<int:video_db_id>/playlists')
def api_video_playlists(video_db_id):
    try:
        db = get_db()
        all_playlists = db.execute(
            'SELECT * FROM playlists ORDER BY name COLLATE NOCASE'
        ).fetchall()
        member_rows = db.execute(
            'SELECT playlist_id FROM playlist_videos WHERE video_id = ?', (video_db_id,)
        ).fetchall()
        member_ids = {r['playlist_id'] for r in member_rows}
        result = [
            {'id': p['id'], 'name': p['name'], 'member': p['id'] in member_ids}
            for p in all_playlists
        ]
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/timeline_offset/<int:video_db_id>', methods=['POST'])
def api_timeline_offset(video_db_id):
    """Permanently apply a time offset (seconds) to all segments of a video."""
    try:
        data = request.get_json(silent=True)
        if not data or 'offset' not in data:
            return jsonify({'success': False, 'error': 'Thiếu tham số offset.'}), 400
        offset = float(data['offset'])
        if offset == 0:
            return jsonify({'success': True, 'rows_updated': 0, 'message': 'Offset = 0, không thay đổi.'})
        db = get_db()
        rows = apply_timeline_offset(video_db_id, offset, db)
        return jsonify({'success': True, 'rows_updated': rows, 'offset_applied': offset})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    with app.app_context():
        init_db()
    app.run(debug=True, host='0.0.0.0', port=5015)
