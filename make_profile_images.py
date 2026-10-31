"""
🖼️ アイコン(400x400)とヘッダー(1500x500)を、AIで作った元画像から正しいサイズに整形する（無料・Pillowのみ）

使い方:
  pip install pillow
  python make_profile_images.py --icon face.png    --focus 0.5,0.38 --zoom 1.0
  python make_profile_images.py --header wide.png  --focus 0.7,0.45 --tagline "毎日の日常をお届け 朝・昼・夜に更新" --font NotoSansCJK-Bold.ttc
  python make_profile_images.py --header wide.png  --preview      # 安全領域ガイド付きの確認画像も出力

  --focus x,y : 残したい中心位置（0〜1。顔の位置を指定する）。--zoom 1.0以上で拡大
  --preview   : ヘッダーにプロフィールアイコンが重なる位置（赤）と、主役を置く中央帯（緑）を描いた確認用画像を追加出力
出力: outputs_profile/icon_400.png, header_1500x500.png (, header_preview.png)
"""
import argparse
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT = 'outputs_profile'


def cover_crop(img, w, h, fx=0.5, fy=0.5, zoom=1.0):
    """w:h の比率で切り出す（fx,fy を中心に）。足りない場合は拡大してから切り出す。"""
    sw, sh = img.size
    scale = max(w / sw, h / sh) * zoom
    nw, nh = int(sw * scale) + 1, int(sh * scale) + 1
    img = img.resize((nw, nh), Image.LANCZOS)
    cx, cy = fx * nw, fy * nh
    left = min(max(cx - w / 2, 0), nw - w)
    top = min(max(cy - h / 2, 0), nh - h)
    return img.crop((int(left), int(top), int(left) + w, int(top) + h))


def parse_focus(s):
    try:
        x, y = [float(v) for v in s.split(',')]
        return min(max(x, 0), 1), min(max(y, 0), 1)
    except Exception:
        return 0.5, 0.5


def make_icon(path, focus, zoom):
    img = Image.open(path).convert('RGB')
    fx, fy = focus
    icon = cover_crop(img, 400, 400, fx, fy, zoom)
    os.makedirs(OUT, exist_ok=True)
    icon.save(f'{OUT}/icon_400.png')
    # 丸く切り抜かれた見え方の確認用
    mask = Image.new('L', (400, 400), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 399, 399), fill=255)
    prev = Image.new('RGB', (400, 400), (240, 240, 240))
    prev.paste(icon, (0, 0), mask)
    prev.save(f'{OUT}/icon_circle_preview.png')
    print(f'✅ {OUT}/icon_400.png（丸く表示される確認用: icon_circle_preview.png）')


def strip_emoji(text):
    """CJKフォントは絵文字を描けず四角（豆腐）になるため、画像内の文字からは絵文字を除く。"""
    kept = ''.join(c for c in text if not (0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF or ord(c) in (0xFE0F, 0x200D)))
    if kept != text:
        print('ℹ️ 絵文字は画像内の文字では表示できないため除きました（プロフィール文には使えます）。')
    return kept.strip()


def draw_tagline(img, text, font_path, size, pos='left'):
    """タグラインを描く。既定は左上（主役を右寄りに置く前提。左下はプロフィールアイコンと重なるため避ける）。"""
    text = strip_emoji(text)
    if not text:
        return img
    d = ImageDraw.Draw(img, 'RGBA')
    try:
        font = ImageFont.truetype(font_path, size, index=0) if font_path else ImageFont.load_default()
    except Exception as e:
        print(f'⚠️ フォントを読めません（{e}）。文字入れをスキップします。')
        return img
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = 70 if pos == 'left' else img.width - tw - 90
    y = 80
    d.rounded_rectangle((x - 24, y - 16, x + tw + 24, y + th + 24), radius=18, fill=(255, 255, 255, 150))
    d.text((x, y - bbox[1]), text, font=font, fill=(60, 50, 70, 255))
    return img


def make_header(path, focus, zoom, tagline, font_path, preview, pos='left'):
    img = Image.open(path).convert('RGB')
    fx, fy = focus
    header = cover_crop(img, 1500, 500, fx, fy, zoom)
    if tagline:
        header = draw_tagline(header, tagline, font_path, 44, pos)
    os.makedirs(OUT, exist_ok=True)
    header.save(f'{OUT}/header_1500x500.png')
    print(f'✅ {OUT}/header_1500x500.png')
    if preview:
        pv = header.copy()
        d = ImageDraw.Draw(pv, 'RGBA')
        # 目安: スマホ表示でプロフィールアイコンが重なる左下（概算）
        d.rectangle((30, 290, 330, 500), fill=(255, 0, 0, 90), outline=(255, 0, 0, 255), width=3)
        # 目安: 端末によって上下が切られるため、主役は中央帯に置く（概算）
        d.rectangle((250, 70, 1450, 430), outline=(0, 170, 0, 255), width=3)
        d.text((40, 300), 'ICON', fill=(255, 255, 255, 255))
        d.text((260, 76), 'SAFE', fill=(255, 255, 255, 255))
        pv.save(f'{OUT}/header_preview.png')
        print(f'✅ {OUT}/header_preview.png（赤=アイコンが重なる目安 / 緑=主役を置く安全領域の目安・どちらも概算）')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--icon')
    ap.add_argument('--header')
    ap.add_argument('--focus', default='0.5,0.4')
    ap.add_argument('--zoom', type=float, default=1.0)
    ap.add_argument('--tagline', default='')
    ap.add_argument('--font', default='')
    ap.add_argument('--text-pos', default='left', choices=['left', 'right'], help='タグラインの位置（既定: 左上。主役は右寄りに置く）')
    ap.add_argument('--preview', action='store_true')
    a = ap.parse_args()
    if not a.icon and not a.header:
        ap.error('--icon か --header のどちらか（または両方）を指定してください')
    f = parse_focus(a.focus)
    if a.icon:
        make_icon(a.icon, f, a.zoom)
    if a.header:
        make_header(a.header, f, a.zoom, a.tagline, a.font, a.preview, a.text_pos)
