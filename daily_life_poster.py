"""
🌸 AIキャラの「日常」自動投稿（無料構成：GitHub Actions + Buffer無料プラン）
====================================================================
images/ フォルダに置いた画像を、朝・昼・夜の3枠（JST）に1枚ずつ、Buffer経由でXへ予約投稿する。

■ Xの新アルゴリズム（2026年10月時点の解説記事ベース）に合わせた設計
  ・元投稿のみ（返信・リポストはフォロー外に届かない）／本文にURL・ハッシュタグを入れない
  ・1枠1投稿（連投すると2本目0.625倍・3本目0.4375倍）／枠は 7-8時・12-13時・20-22時
  ・画像は露出のない全年齢向け日常カットのみ（成人向け判定の連続でアカウント全体が非表示になるため）
  ・投稿時刻は枠内でランダム（定時投稿のBot感を避ける）
  ・返信を集めやすい問いかけを混ぜた投稿文（返信は評価が高い反応）。投稿文は captions.json で編集可能

■ 画像の置き方
  images/ に画像（jpg/png/webp）を入れてコミットする。ファイル名に morning / noon / night を含めると
  その時間帯の枠に優先して使われる（例: 001_morning_cafe.jpg）。含めなければどの枠にも使われる。
  同名の .txt（例: 001_morning_cafe.txt）があれば、その中身を投稿文として使う。
  ※ 画像は raw.githubusercontent.com から配信するため、リポジトリは Public にするか、
    IMAGE_BASE_URL で別の公開ホスティングを指定する。

■ 環境変数
  BUFFER_API_KEY / BUFFER_CHANNEL_ID : 必須（Buffer）
  GITHUB_REPOSITORY (owner/repo, Actionsが自動設定) / IMAGE_BRANCH(既定main) / IMAGE_BASE_URL(任意)
  POSTS_PER_RUN(既定3) / SLOTS(既定 [["morning",7,8],["noon",12,13],["night",20,22]])
  BUFFER_QUEUE_LIMIT(既定9: Buffer無料プランは1チャンネル10件まで) / DRY_RUN=true で送信せず確認のみ
"""
import os
import re
import sys
import json
import random
import datetime
import requests
from pathlib import Path

BUFFER_API_KEY = os.environ.get('BUFFER_API_KEY', '').strip()
BUFFER_CHANNEL_ID = os.environ.get('BUFFER_CHANNEL_ID', '').strip()
BUFFER_ENDPOINT = 'https://api.buffer.com'
DRY_RUN = os.environ.get('DRY_RUN', 'false').strip().lower() == 'true'

IMAGE_DIR = Path(os.environ.get('IMAGE_DIR', 'images'))
CAPTIONS_FILE = Path(os.environ.get('CAPTIONS_FILE', 'captions.json'))
OUT_DIR = Path(os.environ.get('OUTPUT_DIR', 'outputs'))
LOG_FILE = OUT_DIR / 'daily_life_log.json'

REPO = os.environ.get('GITHUB_REPOSITORY', '').strip()
BRANCH = os.environ.get('IMAGE_BRANCH', 'main').strip()
IMAGE_BASE_URL = os.environ.get('IMAGE_BASE_URL', '').strip() or (
    f'https://raw.githubusercontent.com/{REPO}/{BRANCH}/{IMAGE_DIR.as_posix()}/' if REPO else '')

POSTS_PER_RUN = int(os.environ.get('POSTS_PER_RUN', '3'))
QUEUE_LIMIT = int(os.environ.get('BUFFER_QUEUE_LIMIT', '9'))
TZ_OFFSET = float(os.environ.get('BUFFER_TIMEZONE_OFFSET_HOURS', '9'))
LEAD_MIN = float(os.environ.get('LEAD_MIN', '10'))
try:
    _s = os.environ.get('SLOTS', '').strip()
    SLOTS = [(n, int(a), int(b)) for n, a, b in json.loads(_s)] if _s else [('morning', 7, 8), ('noon', 12, 13), ('night', 20, 22)]
except Exception:
    print('⚠️ SLOTSの形式が不正です。既定値（朝7-8時・昼12-13時・夜20-22時）を使います。')
    SLOTS = [('morning', 7, 8), ('noon', 12, 13), ('night', 20, 22)]
IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp'}

_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id status dueAt } }
    ... on MutationError { message }
  }
}
"""

# ---------------- 文字数（Xの重み付きカウント） ----------------
_LOW = [(0, 4351), (8192, 8205), (8208, 8223), (8242, 8247)]


def x_len(text):
    return sum(1 if any(a <= ord(c) <= b for a, b in _LOW) else 2 for c in text)


# ---------------- ログ ----------------
def load_log():
    try:
        if LOG_FILE.exists():
            d = json.loads(LOG_FILE.read_text(encoding='utf-8'))
            if isinstance(d, list):
                return d
    except Exception as e:
        print(f'⚠️ ログ読込に失敗: {e}')
    return []


def save_log(log):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    del log[:-200]
    LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding='utf-8')


def parse_utc(s):
    try:
        return datetime.datetime.strptime(str(s)[:19], '%Y-%m-%dT%H:%M:%S')
    except Exception:
        return None


# ---------------- 枠（1枠1投稿） ----------------
def free_slots(log, now_utc, count):
    """まだ予約が入っていない将来の枠を時系列順に count 個返す: [(slot_name, due_utc)]"""
    local_now = now_utc + datetime.timedelta(hours=TZ_OFFSET)
    earliest = local_now + datetime.timedelta(minutes=LEAD_MIN)
    occupied = set()
    for e in log:
        d = parse_utc(e.get('due_at'))
        if d:
            ld = d + datetime.timedelta(hours=TZ_OFFSET)
            occupied.add((ld.date(), e.get('slot')))
    out = []
    for off in range(0, 15):
        day = local_now.date() + datetime.timedelta(days=off)
        for name, s_h, e_h in sorted(SLOTS, key=lambda x: x[1]):
            if (day, name) in occupied:
                continue
            ws = datetime.datetime.combine(day, datetime.time(hour=s_h))
            we = datetime.datetime.combine(day, datetime.time(hour=e_h))
            lo = max(ws, earliest)
            if (we - lo).total_seconds() < 600:
                continue
            due = lo + datetime.timedelta(seconds=random.uniform(0, (we - lo).total_seconds()))
            out.append((name, due - datetime.timedelta(hours=TZ_OFFSET)))
            if len(out) >= count:
                return out
    return out


# ---------------- 画像・投稿文 ----------------
def list_unused_images(log):
    used = {e.get('image') for e in log}
    names = [n for n, _, _ in SLOTS]
    imgs = sorted(p for p in IMAGE_DIR.glob('*') if p.suffix.lower() in IMG_EXT and p.name not in used)

    def label(p):
        return next((n for n in names if n in p.stem.lower()), None)
    return [(p, label(p)) for p in imgs]


def pick_image(unused, slot, taken):
    for p, lab in unused:
        if p.name not in taken and lab == slot:
            return p
    for p, lab in unused:
        if p.name not in taken and lab is None:
            return p
    return None


def load_captions():
    try:
        return json.loads(CAPTIONS_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'⚠️ captions.json を読めません（{e}）。')
        return {}


def pick_caption(img_path, slot, pools, log):
    side = img_path.with_suffix('.txt')
    if side.exists():
        t = side.read_text(encoding='utf-8').strip()
        if t:
            return t
    pool = [c for c in (pools.get(slot) or []) + (pools.get('any') or []) if isinstance(c, str)]
    used = {e.get('caption') for e in log[-60:]}
    fresh = [c for c in pool if c not in used] or pool  # 使い切ったら再利用
    return random.choice(fresh) if fresh else ''


def sanitize(text):
    """URL・ハッシュタグ・@メンションを除去（本文リンク/タグはリーチ低下やスパム判定の原因になるため）。"""
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'[#＃]\S+', '', text)
    text = re.sub(r'[@＠]\w+', '', text)
    return re.sub(r'[ \t]{2,}', ' ', text).strip()


# ---------------- Buffer ----------------
def buffer_request(query, variables):
    try:
        r = requests.post(BUFFER_ENDPOINT, json={'query': query, 'variables': variables},
                          headers={'Authorization': f'Bearer {BUFFER_API_KEY}', 'Content-Type': 'application/json'}, timeout=30)
        d = r.json()
    except Exception as e:
        return None, f'通信エラー: {e}'
    if d.get('errors'):
        return None, '; '.join(x.get('message', str(x)) for x in d['errors'])
    return d.get('data'), None


def send_post(text, image_url, due_utc):
    item = {'text': text, 'assets': [{'image': {'url': image_url}}]}
    inp = {
        'text': text, 'channelId': BUFFER_CHANNEL_ID, 'schedulingType': 'automatic',
        'mode': 'customScheduled', 'dueAt': due_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        'assets': item['assets'], 'metadata': {'twitter': {'thread': [item]}},
    }
    data, err = buffer_request(_MUTATION, {'input': inp})
    if err:
        return False, err
    res = (data or {}).get('createPost') or {}
    if res.get('message'):
        return False, res['message']
    return bool(res.get('post')), None if res.get('post') else '応答にpostがありません'


def image_reachable(url):
    """画像URLがBufferから取得できるか（GitHubへ未pushだと404になる）。アフィリエイトURL等ではなく自分の画像URLのみ確認する。"""
    try:
        r = requests.head(url, timeout=10, allow_redirects=True)
        return r.status_code < 400
    except Exception:
        return False


# ---------------- メイン ----------------
def main():
    if not DRY_RUN and (not BUFFER_API_KEY or not BUFFER_CHANNEL_ID):
        print('❌ BUFFER_API_KEY / BUFFER_CHANNEL_ID が未設定です。')
        sys.exit(1)
    if not IMAGE_BASE_URL:
        print('❌ 画像のURLを決められません。GITHUB_REPOSITORY か IMAGE_BASE_URL を設定してください。')
        sys.exit(1)

    now = datetime.datetime.utcnow()
    log = load_log()
    pending = sum(1 for e in log if (parse_utc(e.get('due_at')) or now) > now)
    room = QUEUE_LIMIT - pending
    print(f'📋 予約待ち {pending}件 / Bufferの上限 {QUEUE_LIMIT}件 → 今回追加できるのは最大 {max(room, 0)}件')
    if room <= 0:
        print('ℹ️ キューが上限に達しているため何もしません。')
        return

    unused = list_unused_images(log)
    print(f'🖼️ 未使用の画像: {len(unused)}枚（{IMAGE_DIR}/）')
    if not unused:
        print('⚠️ 投稿できる画像がありません。images/ に画像を追加してコミットしてください。')
        return

    pools = load_captions()
    taken, posted = set(), 0
    for slot, due in free_slots(log, now, min(POSTS_PER_RUN, room)):
        img = pick_image(unused, slot, taken)
        if img is None:
            print(f'⚠️ {slot}枠に使える画像がありません（ここで終了）。')
            break
        taken.add(img.name)
        caption = sanitize(pick_caption(img, slot, pools, log))
        if x_len(caption) > 280:
            print(f'⚠️ 投稿文が280字を超えています。スキップ: {img.name}')
            continue
        url = IMAGE_BASE_URL.rstrip('/') + '/' + img.name
        local = (due + datetime.timedelta(hours=TZ_OFFSET)).strftime('%m/%d %H:%M')
        print(f'\n--- {slot}枠 / JST {local} / {img.name} ---\n{caption}')
        if DRY_RUN:
            continue
        if not image_reachable(url):
            print(f'  ⚠️ 画像URLを取得できません（未pushまたはリポジトリが非公開）: {url}')
            continue
        ok, err = send_post(caption, url, due)
        if not ok:
            print(f'  ❌ Buffer送信失敗: {err}')
            continue
        print('  ✅ Bufferに予約しました')
        log.append({'image': img.name, 'slot': slot, 'caption': caption,
                    'due_at': due.strftime('%Y-%m-%dT%H:%M:%S.000Z')})
        save_log(log)
        posted += 1

    print(f'\n✅ 完了: {posted}件を予約しました。')


if __name__ == '__main__':
    main()
