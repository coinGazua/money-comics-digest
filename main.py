import os
import json
import datetime
import subprocess
import http.cookiejar
import requests
import time
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
import anthropic

YOUTUBE_API_KEY = os.environ.get('YOUTUBE_API_KEY', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
CHANNEL_ID = 'UCJo6G1u0e_-wS-JQn3T-zEw'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_PATH = os.path.join(BASE_DIR, 'cookies.txt')
DATA_DIR = os.path.join(BASE_DIR, 'data')

# ─────────────────────────────────────────
# 유튜브
# ─────────────────────────────────────────

# .env 자동 로드
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(env_path):
    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and '=' in line:
                key, val = line.split('=', 1)
                os.environ.setdefault(key.strip(), val.strip())

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
# 자막 (재시도 3회, 5분 간격)
# ─────────────────────────────────────────

def get_transcript(video_id, retry=3, retry_interval=300):
    session = requests.Session()
    if os.path.exists(COOKIE_PATH):
        cj = http.cookiejar.MozillaCookieJar(COOKIE_PATH)
        try:
            cj.load(ignore_discard=True, ignore_expires=True)
            session.cookies = cj
            print(f"  쿠키 로드 완료")
        except Exception as e:
            print(f"  ⚠️ 쿠키 로드 실패: {e} — 쿠키 재발급 필요")
    else:
        print(f"  ⚠️ 쿠키 파일 없음: {COOKIE_PATH}")

    api = YouTubeTranscriptApi(http_client=session)

    for attempt in range(1, retry + 1):
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
            print("  ⚠️ 자막 비활성화 — 채널 설정 확인 필요")
            return None
        except NoTranscriptFound:
            if attempt < retry:
                print(f"  ⚠️ 자막 없음 (시도 {attempt}/{retry}) — {retry_interval//60}분 후 재시도 (라이브 종료 후 자막 생성 딜레이)")
                time.sleep(retry_interval)
            else:
                print(f"  ⚠️ 자막 없음 — {retry}회 시도 후 포기")
                return None
        except VideoUnavailable:
            print("  ⚠️ 영상 접근 불가 — 비공개 또는 삭제")
            return None
        except Exception as e:
            err_msg = str(e)
            if 'RequestBlocked' in err_msg or 'IPBlocked' in err_msg:
                print("  ⚠️ YouTube IP 차단 — 쿠키가 만료됐거나 IP가 차단됨. 쿠키 재발급 필요")
                return None
            if attempt < retry:
                print(f"  ⚠️ 자막 추출 실패 (시도 {attempt}/{retry}): {type(e).__name__} — 재시도 중...")
                time.sleep(10)
            else:
                print(f"  ⚠️ 자막 추출 실패 — {retry}회 시도 후 포기: {e}")
                return None

# ─────────────────────────────────────────
# 요약 (재시도 3회, 10초 간격)
# ─────────────────────────────────────────

def load_prompt():
    try:
        prompt_path = os.path.join(BASE_DIR, 'config', 'prompt.txt')
        with open(prompt_path, 'r', encoding='utf-8') as f:
            return f.read()
    except:
        print("⚠️ config/prompt.txt 없음 — 기본 프롬프트 사용")
        return get_default_prompt()

def get_default_prompt():
    return """다음은 머니코믹스 유튜브 라이브 방송의 자막입니다. 아래 양식에 맞춰 한국어로 요약해주세요.

[방송명] (자막에서 유추)
[날짜] (자막에서 유추)
[진행자] (자막에서 유추)

한줄 요약: 이번 방송의 핵심 메시지를 1~2문장으로 요약

① 오늘의 핵심 매크로 뷰
  - 이번 방송에서 진행자와 게스트가 강조한 시장 전망 1~3줄

② 국내장 / 미국장 시황
  - 국내장: 주요 코멘트 요약
  - 미국장: 주요 코멘트 요약

③ 내 포지션 관련 뷰
  - 암호화폐 전반: 방송에서 암호화폐 시장 전반에 대해 언급한 내용 요약. 언급 없으면 N/A
  - 코스닥150 ETF: 방송에서 코스닥 전반 또는 코스닥150에 대해 언급한 내용 요약. 언급 없으면 N/A

④ 기타 언급 종목 / 자산
  - 종목명: 해당 코멘트 한 줄 (③ 제외한 나머지, 출연자 개인 보유 종목 제외)

⑤ 주목할 발언
  - 투자 판단에 영향을 줄 수 있는 발언 1~3개

⑥ 키워드
  - 이번 방송의 핵심 단어 5개 (쉼표로 구분)

[필수 규칙]
- 반드시 위 양식 순서대로 출력할 것
- 마크다운 표, 소제목(###), 볼드(**) 등 서식 사용 금지
- ③번은 언급 없어도 반드시 N/A 표시
- 출연자 개인 보유 종목이나 개인 투자 경험은 ④에 포함하지 말 것
- 투자 판단에 실질적으로 유용한 정보 위주로 작성

자막:
"""

def summarize_with_claude(transcript, prompt, retry=3, retry_interval=10):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    for attempt in range(1, retry + 1):
        try:
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt + transcript[:50000]}]
            )
            return message.content[0].text
        except anthropic.OverloadedError:
            if attempt < retry:
                print(f"  ⚠️ Claude API 과부하 (시도 {attempt}/{retry}) — {retry_interval}초 후 재시도")
                time.sleep(retry_interval)
            else:
                print(f"  ⚠️ Claude API 과부하 — {retry}회 시도 후 포기")
                return None
        except anthropic.BadRequestError as e:
            if 'credit' in str(e).lower():
                print(f"  ⚠️ Claude API 크레딧 부족 — console.anthropic.com에서 충전 필요")
            else:
                print(f"  ⚠️ Claude API 오류: {e}")
            return None
        except Exception as e:
            if attempt < retry:
                print(f"  ⚠️ Claude API 오류 (시도 {attempt}/{retry}): {e} — 재시도 중...")
                time.sleep(retry_interval)
            else:
                print(f"  ⚠️ Claude API 오류 — {retry}회 시도 후 포기: {e}")
                return None

# ─────────────────────────────────────────
# 저장
# ─────────────────────────────────────────

def already_processed(video_id):
    if not os.path.exists(DATA_DIR):
        return False
    for filename in os.listdir(DATA_DIR):
        if filename.endswith('.json') and filename != 'index.json':
            try:
                with open(os.path.join(DATA_DIR, filename), 'r', encoding='utf-8') as f:
                    items = json.load(f)
                    if not isinstance(items, list):
                        items = [items]
                    if any(i.get('video_id') == video_id for i in items):
                        return True
            except:
                pass
    return False

def save_result(video):
    os.makedirs(DATA_DIR, exist_ok=True)
    filename = os.path.join(DATA_DIR, f'{video["date_str"]}.json')
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
    git_push()

def update_index():
    dates = sorted(
        [f.replace('.json', '') for f in os.listdir(DATA_DIR)
         if f.endswith('.json') and f != 'index.json'],
        reverse=True
    )
    with open(os.path.join(DATA_DIR, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump({'dates': dates}, f, ensure_ascii=False, indent=2)

def git_push():
    try:
        remote_url = f'https://{GITHUB_TOKEN}@github.com/coinGazua/money-comics-digest.git'
        subprocess.run(['git', 'remote', 'set-url', 'origin', remote_url], cwd=BASE_DIR, check=True)
        # 항상 최신 remote 상태로 리셋 후 data/만 추가
        subprocess.run(['git', 'fetch', 'origin', 'main'], cwd=BASE_DIR, check=True)
        subprocess.run(['git', 'reset', '--mixed', 'origin/main'], cwd=BASE_DIR, check=True)
        subprocess.run(['git', 'add', 'data/'], cwd=BASE_DIR, check=True)
        # 변경사항 없으면 커밋 스킵
        result = subprocess.run(['git', 'diff', '--staged', '--quiet'], cwd=BASE_DIR)
        if result.returncode == 0:
            print("  ℹ️ 변경사항 없음 — push 스킵")
            return
        subprocess.run(['git', 'commit', '-m', f'digest: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")} KST'], cwd=BASE_DIR, check=True)
        subprocess.run(['git', 'push', 'origin', 'main'], cwd=BASE_DIR, check=True)
        print("  🚀 GitHub push 완료")
    except Exception as e:
        print(f"  ⚠️ Git push 실패: {e}")

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
    summary = summarize_with_claude(transcript, load_prompt())
    if not summary:
        print("  ❌ 요약 실패 — 스킵")
        return False
    video['summary'] = summary
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
