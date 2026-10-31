"""
🌸 AIキャラの「日常」自動投稿（無料構成：GitHub Actions + Buffer無料プラン）
====================================================================
images/ フォルダに置いた画像を、朝・昼・夜の3枠（JST）に1枚ずつ、Buffer経由でXへ予約投稿する。

■ 投稿の基本方針（v2）
  ・基本は「日常のひとこと」のテキスト投稿（朝・昼・夜の3枠）。画像付きは IMAGE_INTERVAL_DAYS 日に1回（既定3日）だけ、
    IMAGE_SLOT の枠（既定: 夜）に付ける。画像は週に2〜3枚用意すれば足りる。
  ・画像がない日は自動的にテキストのみで投稿する（止まらない）。

■ Xの新アルゴリズム（2026年10月時点の解説記事ベース）に合わせた設計
  ・元投稿のみ（返信・リポストはフォロー外に届かない）／本文にURL・ハッシュタグを入れない
  ・1枠1投稿（連投すると2本目0.625倍・3本目0.4375倍）／枠は 7-8時・12-13時・20-22時
  ・画像は露出のない全年齢向けの日常カットのみ（成人向け判定の連続でアカウント全体が非表示になるため）
  ・投稿時刻は枠内でランダム（定時投稿のBot感を避ける）
  ・返信を集めやすい問いかけを混ぜた投稿文（返信は評価が高い反応）。投稿文は captions.json で編集可能
  ・同じ文章の使い回しはスパム扱いされやすいため、直近90投稿以内の文は再利用しない

■ 画像と投稿文（captions.json）
  images/ に画像（jpg/png/webp）を入れてコミットする。ファイル名は半角英数字を推奨（日本語名はmacOS等で表記ゆれが起きやすい）。
  captions.json の構成:
    "themes": [{"id","slots":["morning"|"noon"|"night"|"any"],"captions":[...]}]
        … テキストのみ投稿の素材。直近10投稿と同じテーマは避け、直近90投稿と同じ文も使わない。
    "images": {"ファイル名": {"slot":"night","captions":[...]}}
        … 画像ごとの専用投稿文（画像付き投稿で使う）。"slot" は「この画像に合う時間帯」で、IMAGE_SLOT と同じ画像が優先される。
           "skip": true の画像は自動投稿しない（アイコン・固定ポスト用など）。
    "photo": [...]  … images に登録のない画像用の汎用文。
  同名の .txt（例: 01_cafe.txt）があれば、そちらが最優先で使われる。
  ※ 画像は raw.githubusercontent.com から配信するため、リポジトリは Public にするか、
    IMAGE_BASE_URL で別の公開ホスティングを指定する。

■ 環境変数
  BUFFER_API_KEY / BUFFER_CHANNEL_ID : 必須（Buffer）
  GITHUB_REPOSITORY (owner/repo, Actionsが自動設定) / IMAGE_BRANCH(既定main) / IMAGE_BASE_URL(任意)
  POSTS_PER_RUN(既定3) / SLOTS(既定 [["morning",7,8],["noon",12,13],["night",20,22]])
  IMAGE_INTERVAL_DAYS(既定3: 画像付き投稿の間隔・日) / IMAGE_SLOT(既定night: 画像を付ける枠) / 0にすると画像は一切付けない
  BUFFER_QUEUE_LIMIT(既定9: Buffer無料プランは1チャンネル10件まで) / DRY_RUN=true で送信せず確認のみ
"""
import os
import re
import sys
import unicodedata
from urllib.parse import quote
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
IMAGE_INTERVAL_DAYS = int(os.environ.get('IMAGE_INTERVAL_DAYS', '3'))  # 0なら画像は付けない
IMAGE_SLOT = os.environ.get('IMAGE_SLOT', 'night').strip()
CAPTION_NO_REPEAT = 90  # 直近この件数の投稿と同じ文は使わない
THEME_NO_REPEAT = 10    # 直近この件数のテキスト投稿と同じテーマは避ける

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


def local_date(due_utc):
    return (due_utc + datetime.timedelta(hours=TZ_OFFSET)).date()


def last_image_date(log):
    """ログ上で最後に画像付きで予約した日（現地日付）。旧形式のログ（has_imageなし）は画像付き扱い。"""
    ds = []
    for e in log:
        d = parse_utc(e.get('due_at'))
        if d and e.get('has_image', True):
            ds.append(local_date(d))
    return max(ds) if ds else None


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
def nfc(t):
    return unicodedata.normalize('NFC', t)


def image_meta(data):
    """captions.json の images を、ファイル名（NFC正規化）をキーにした辞書にする。"""
    return {nfc(k): v for k, v in (data.get('images') or {}).items() if isinstance(v, dict)}


def list_unused_images(log, data):
    used = {nfc(e.get('image') or '') for e in log}
    names = [n for n, _, _ in SLOTS]
    meta = image_meta(data)
    imgs = sorted(p for p in IMAGE_DIR.glob('*')
                  if p.suffix.lower() in IMG_EXT and nfc(p.name) not in used
                  and not (meta.get(nfc(p.name)) or {}).get('skip'))

    def label(p):
        hit = next((n for n in names if n in p.stem.lower()), None)
        if hit:
            return hit
        m_slot = (meta.get(nfc(p.name)) or {}).get('slot')
        return m_slot if m_slot in names else None
    return [(p, label(p)) for p in imgs]


def pick_image(unused, slot, taken):
    for p, lab in unused:
        if p.name not in taken and lab == slot:
            return p
    for p, lab in unused:
        if p.name not in taken and lab is None:
            return p
    for p, lab in unused:  # 他の枠名が付いた画像も、余っていれば使う
        if p.name not in taken:
            return p
    return None


def load_captions():
    try:
        return json.loads(CAPTIONS_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'⚠️ captions.json を読めません（{e}）。')
        return {}


def _recent_captions(log):
    return [e.get('caption') for e in log[-CAPTION_NO_REPEAT:]]


def _least_recent(pool, recent):
    def last_pos(c):
        return max((i for i, r in enumerate(recent) if r == c), default=-1)
    return min(pool, key=last_pos)


def _theme_slots(t):
    sl = t.get('slots', t.get('slot', 'any'))
    return [sl] if isinstance(sl, str) else list(sl)


def pick_image_caption(img_path, data, log):
    """画像付き投稿の文: ①同名.txt ②captions.json の images[ファイル名] ③汎用の photo。戻り値は (文, テーマID)。"""
    side = img_path.with_suffix('.txt')
    if side.exists():
        t = side.read_text(encoding='utf-8').strip()
        if t:
            return t, None
    recent = _recent_captions(log)
    pool = [c for c in ((image_meta(data).get(nfc(img_path.name)) or {}).get('captions') or []) if isinstance(c, str)]
    if not pool:
        pool = [c for c in (data.get('photo') or []) if isinstance(c, str)]
    if not pool:
        return '', None
    fresh = [c for c in pool if c not in recent]
    return (random.choice(fresh) if fresh else _least_recent(pool, recent)), None


def pick_text_caption(slot, data, log):
    """テキストのみ投稿の文。その時間帯に合うテーマから、直近と重ならないテーマ・文を選ぶ。戻り値は (文, テーマID)。"""
    recent = _recent_captions(log)
    recent_themes = [e.get('theme') for e in log if e.get('theme')][-THEME_NO_REPEAT:]
    cand = []
    for t in data.get('themes') or []:
        slots = _theme_slots(t)
        if slot in slots or 'any' in slots:
            fresh = [c for c in (t.get('captions') or []) if isinstance(c, str) and c not in recent]
            if fresh:
                cand.append((t, fresh))
    total_fresh = sum(len(f) for _, f in cand)
    if total_fresh <= 5:
        print(f'⚠️ {slot}枠で使える未使用の投稿文が残り少なくなっています（{total_fresh}件）。captions.json のthemesに追加してください。')
    if cand:
        pref = [x for x in cand if x[0].get('id') not in recent_themes] or cand
        t, fresh = random.choice(pref)
        return random.choice(fresh), t.get('id')
    # themes を使い切った／未定義: 旧形式のプール（slot名・any）または最も昔に使った文で代替
    pool = [c for c in (data.get(slot) or []) + (data.get('any') or []) if isinstance(c, str)]
    if not pool:
        pool = [c for t in (data.get('themes') or []) if slot in _theme_slots(t) or 'any' in _theme_slots(t)
                for c in (t.get('captions') or []) if isinstance(c, str)]
    if not pool:
        return '', None
    fresh = [c for c in pool if c not in recent]
    return (random.choice(fresh) if fresh else _least_recent(pool, recent)), None


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
    """image_url が None ならテキストのみの投稿。"""
    item = {'text': text}
    if image_url:
        item['assets'] = [{'image': {'url': image_url}}]
    inp = {
        'text': text, 'channelId': BUFFER_CHANNEL_ID, 'schedulingType': 'automatic',
        'mode': 'customScheduled', 'dueAt': due_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        'metadata': {'twitter': {'thread': [item]}},
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

    use_images = IMAGE_INTERVAL_DAYS > 0
    if use_images and IMAGE_SLOT not in [n for n, _, _ in SLOTS]:
        print(f'⚠️ IMAGE_SLOT「{IMAGE_SLOT}」が SLOTS に存在しないため、画像は付けません。')
        use_images = False
    if use_images and not IMAGE_BASE_URL:
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

    pools = load_captions()
    unused = list_unused_images(log, pools) if use_images else []
    last_img = last_image_date(log)
    print(f'🖼️ 画像付きは{IMAGE_INTERVAL_DAYS}日に1回（{IMAGE_SLOT}枠）。未使用の画像: {len(unused)}枚 / 前回の画像付き投稿: {last_img or "なし"}')

    taken, posted, n_img = set(), 0, 0
    for slot, due in free_slots(log, now, min(POSTS_PER_RUN, room)):
        d = local_date(due)
        want_image = use_images and slot == IMAGE_SLOT and (last_img is None or (d - last_img).days >= IMAGE_INTERVAL_DAYS)
        img = pick_image(unused, slot, taken) if want_image else None
        if want_image and img is None:
            print(f'ℹ️ {d} の{slot}枠は画像付きの日ですが、未使用の画像がないためテキストのみにします（images/ に画像を追加してください）。')
        if img is not None:
            taken.add(img.name)

        raw, theme = pick_image_caption(img, pools, log) if img is not None else pick_text_caption(slot, pools, log)
        caption = sanitize(raw)
        if not caption:
            print(f'⚠️ {slot}枠の投稿文がありません（captions.json を確認）。スキップします。')
            continue
        if x_len(caption) > 280:
            print(f'⚠️ 投稿文が280字を超えています。スキップ: {caption[:20]}…')
            continue

        url = (IMAGE_BASE_URL.rstrip('/') + '/' + quote(img.name)) if img is not None else None
        local = (due + datetime.timedelta(hours=TZ_OFFSET)).strftime('%m/%d %H:%M')
        kind = f'📷画像付き（{img.name}）' if img is not None else 'テキストのみ'
        print(f'\n--- {slot}枠 / JST {local} / {kind} ---\n{caption}')
        if DRY_RUN:
            if img is not None:
                last_img = d  # ドライランでも間隔の計算は進めて、以降の計画を正しく表示する
            continue

        if img is not None and not image_reachable(url):
            print(f'  ⚠️ 画像URLを取得できません（未pushまたはリポジトリが非公開）。この枠はテキストのみにします: {url}')
            img, url = None, None
            raw, theme = pick_text_caption(slot, pools, log)
            caption = sanitize(raw)
            if not caption:
                continue

        ok, err = send_post(caption, url, due)
        if not ok:
            print(f'  ❌ Buffer送信失敗: {err}')
            continue
        print('  ✅ Bufferに予約しました')
        has_image = img is not None
        log.append({'image': img.name if has_image else None, 'has_image': has_image, 'slot': slot,
                    'theme': theme, 'caption': caption, 'due_at': due.strftime('%Y-%m-%dT%H:%M:%S.000Z')})
        save_log(log)
        posted += 1
        if has_image:
            n_img += 1
            last_img = d

    print(f'\n✅ 完了: {posted}件を予約しました（うち画像付き {n_img}件）。')


if __name__ == '__main__':
    main()
