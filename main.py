import os
import json
import datetime
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
import anthropic

YOUTUBE_API_KEY = os.environ['YOUTUBE_API_KEY']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
CHANNEL_ID = 'UCJo6G1u0e_-wS-JQn3T-zEw'

# ─────────────────────────────────────────
# 유튜브
# ─────────────────────────────────────────

def get_video_id_from_url(url):
    import re
    patterns = [
        r'youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'youtu\.be/([a-zA-Z0-9_-]{11})',
        r'youtube\.com/live/([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def get_video_info(video_id):
    youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
    response = youtube.videos().list(part='snippet', id=video_id).execute()
    if not response['items']:
        return None
    item = response['items'][0]
    dt = datetime.datetime.fromisoformat(item['snippet']['publishedAt'].replace('Z', '+00:00'))
    kst = dt + datetime.timedelta(hours=9)
    return {
        'id': video_id,
        'title': item['snippet']['title'],
        'date_str': kst.strftime('%Y-%m-%d')
    }

def get_completed_lives():
    youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
    response = youtube.search().list(
        part='snippet',
        channelId=CHANNEL_ID,
        order='date',
        type='video',
        eventType='completed',
        maxResults=10
    ).execute()
    videos = []
    for item in response['items']:
        dt = datetime.datetime.fromisoformat(item['snippet']['publishedAt'].replace('Z', '+00:00'))
        kst = dt + datetime.timedelta(hours=9)
        videos.append({
            'id': item['id']['videoId'],
            'title': item['snippet']['title'],
            'date_str': kst.strftime('%Y-%m-%d')
        })
    return videos

# ─────────────────────────────────────────
# 자막
# ─────────────────────────────────────────

def get_transcript(video_id):
    api = YouTubeTranscriptApi()
    try:
        transcript_list = api.list(video_id)
        try:
            t = transcript_list.find_transcript(['ko'])
        except:
            try:
                t = transcript_list.find_generated_transcript(['ko', 'en'])
            except:
                t = list(transcript_list)[0]
        data = t.fetch()
        text = ' '.join([s.text for s in data])
        print(f"  자막 추출 성공 | {t.language} | 자동생성: {t.is_generated} | {len(text)}글자")
        return text
    except TranscriptsDisabled:
        print("  ⚠️ 자막 비활성화 — 채널 설정에서 자막이 꺼져 있음")
        return None
    except NoTranscriptFound:
        print("  ⚠️ 자막 없음 — 라이브 종료 후 자막 생성까지 수십 분 소요될 수 있음")
        return None
    except VideoUnavailable:
        print("  ⚠️ 영상 접근 불가 — 비공개 또는 삭제")
        return None
    except Exception as e:
        print(f"  ⚠️ 자막 추출 실패: {type(e).__name__}: {e}")
        return None

# ─────────────────────────────────────────
# 요약
# ─────────────────────────────────────────

def load_prompt():
    try:
        with open('config/prompt.txt', 'r', encoding='utf-8') as f:
            return f.read()
    except:
        print("⚠️ config/prompt.txt 없음 — 기본 프롬프트 사용")
        return get_default_prompt()

def get_default_prompt():
    return """다음은 머니코믹스 유튜브 라이브 방송의 자막입니다. 아래 양식에 맞춰 한국어로 요약해주세요.

[방송명] (자막에서 유추)
[날짜] (자막에서 유추)
[진행자] (자막에서 유추)

① 오늘의 핵심 매크로 뷰
  - 이번 방송에서 가장 강조된 시장 전망 1~3줄

② 국내장 / 미국장 시황
  - 각각 주요 코멘트 요약

③ 내 포지션 관련 뷰
  - 암호화폐 전반: (언급 내용 요약 / N/A)
  - 코스닥150 ETF: (언급 내용 요약 / N/A)

④ 기타 언급 종목 / 자산
  - 종목명 + 코멘트 한 줄 (③ 제외한 나머지)

⑤ 주목할 발언
  - 투자 판단에 영향을 줄 수 있는 발언

⑥ 키워드
  - 이번 방송의 핵심 단어 5개

[필수 규칙]
- 반드시 위 6개 섹션 모두 포함
- ③번은 언급 없어도 반드시 N/A 표시
- 투자 판단에 실질적으로 유용한 정보 위주로 작성

자막:
"""

def summarize_with_claude(transcript, prompt):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt + transcript[:50000]}]
    )
    return message.content[0].text

# ─────────────────────────────────────────
# 저장
# ─────────────────────────────────────────

def already_processed(video_id):
    if not os.path.exists('data'):
        return False
    for filename in os.listdir('data'):
        if filename.endswith('.json') and filename != 'index.json':
            try:
                with open(f'data/{filename}', 'r', encoding='utf-8') as f:
                    items = json.load(f)
                    if not isinstance(items, list):
                        items = [items]
                    if any(i.get('video_id') == video_id for i in items):
                        return True
            except:
                pass
    return False

def save_result(video):
    os.makedirs('data', exist_ok=True)
    filename = f'data/{video["date_str"]}.json'
    existing = []
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            existing = json.load(f)
        if not isinstance(existing, list):
            existing = [existing]
    existing.append({
        'video_id': video['id'],
        'title': video['title'],
        'summary': video['summary'],
        'date': video['date_str'],
        'processed_at': datetime.datetime.now().isoformat()
    })
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    print(f"  💾 저장: {filename}")
    update_index()

def update_index():
    dates = sorted(
        [f.replace('.json', '') for f in os.listdir('data')
         if f.endswith('.json') and f != 'index.json'],
        reverse=True
    )
    with open('data/index.json', 'w', encoding='utf-8') as f:
        json.dump({'dates': dates}, f, ensure_ascii=False, indent=2)

# ─────────────────────────────────────────
# 실행
# ─────────────────────────────────────────

def process_video(video):
    print(f"\n[처리] {video['title']} ({video['id']})")
    if already_processed(video['id']):
        print("  ⏭️ 이미 처리됨 — 스킵")
        return False
    transcript = get_transcript(video['id'])
    if not transcript:
        print("  ❌ 자막 없음 — 스킵")
        return False
    print("  🤖 요약 중...")
    video['summary'] = summarize_with_claude(transcript, load_prompt())
    save_result(video)
    print("  ✅ 완료")
    return True

def run_scheduled():
    print("=== 자동 모드: 채널 라이브 스캔 ===")
    videos = get_completed_lives()
    if not videos:
        print("완료된 라이브 없음")
        return
    for video in videos:
        process_video(video)

def run_manual(url):
    print(f"=== 수동 모드: {url} ===")
    video_id = get_video_id_from_url(url)
    if not video_id:
        print("❌ 유효하지 않은 YouTube URL")
        return
    video = get_video_info(video_id)
    if not video:
        print("❌ 영상 정보 조회 실패")
        return
    process_video(video)

if __name__ == '__main__':
    manual_url = os.environ.get('MANUAL_URL', '').strip()
    if manual_url:
        run_manual(manual_url)
    else:
        run_scheduled()
