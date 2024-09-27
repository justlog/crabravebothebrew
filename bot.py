import logging
import os
from io import BytesIO
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus as url_quote
import subprocess
import tempfile
from queue import Queue
from threading import Thread
import json
from typing import Optional

import telegram
from telegram.ext import MessageHandler, CommandHandler, InlineQueryHandler, filters, CallbackContext, Application, ContextTypes
from PIL import Image, ImageDraw, ImageFont
import libhoney
from flask import Flask, render_template, request
app = Flask(__name__)

libhoney.init(writekey=os.environ.get('HONEYCOMB_KEY'), dataset="crabravebot", debug=True)

font = ImageFont.truetype("assets/fonts/NotoSansHebrew-Regular.ttf", 48)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class Style:
    """Crab rave style"""
    id: str
    name: str
    source: str
    image: Path
    video: Path


def get_styles():
    templates = Path('assets/templates')
    result = []
    for folder in templates.iterdir():
        s_id = folder.name
        with list(folder.glob('*.json'))[0].open('r') as meta:
            metadata = json.load(meta)
            name = metadata['name']
            source = metadata['source']
        image = list(folder.glob('*.png'))[0]
        video = list(folder.glob('*.mp4'))[0]
        result.append(Style(s_id, name, source, image, video))
    result.sort(key=lambda x: x.name)
    return result


STYLES = get_styles()


def word_wrap(text: str, draw: ImageDraw.ImageDraw, width: int) -> str:
    left, top, right, bottom = draw.multiline_textbbox((0,0), text=text, font=font)
    text_width = right-left
    if text_width > width:
        lines = text.splitlines()
        wrapped_text = []
        for line in lines:
            line_width, _ = draw.textsize(line, font=font)
            if line_width > width:
                new_line = ''
                last_line = new_line
                for word in line.split():
                    if len(new_line) > 0:
                        new_line += ' '
                    new_line += word
                    line_width, _ = draw.textsize(new_line, font=font)
                    if line_width > width:
                        wrapped_text.append(last_line)
                        new_line = word
                    last_line = new_line
                if len(last_line) > 0:
                    wrapped_text.append(last_line)
            else:
                wrapped_text.append(line)
        return '\n'.join(wrapped_text)
    else:
        return text


def render_text(text: Optional[str], base: Image):
    if base.mode == 'RGB':
        white = (255, 255, 255)
        black = (0, 0, 0)
    elif base.mode == 'RGBA':
        white = (255, 255, 255, 255)
        black = (0, 0, 0, 255)
    else:
        raise ValueError('Base image {}, not RGB/RGBA'.format(base.mode))

    if text is None:
        text = ''
    draw = ImageDraw.Draw(base)
    text = word_wrap(text, draw, base.width)
    left, top, right, bottom = draw.multiline_textbbox((0,0), text=text, font=font)
    text_width = right-left
    text_height = bottom-top
    center_x = base.width // 2
    center_y = base.height // 2
    draw.multiline_text((center_x - text_width / 2, center_y - text_height / 2), text, font=font,
                        fill=white, stroke_width=1, stroke_fill=black, align='center')


def make_image(text: str, style_id: str):
    outfile = BytesIO()
    style = [s for s in STYLES if s.id == style_id][0]
    with Image.open(style.image) as base:
        render_text(text, base)
        base.save(outfile, 'PNG')
    return outfile.getvalue()


def make_video(text: str, style_id: str):
    ev = libhoney.Event()
    ev.add_field('type', 'bake')
    ev.add_field('text', text)
    ev.add_field('style', style_id)
    ev.send()
    style = [s for s in STYLES if s.id == style_id][0]
    with Image.open(style.image) as image:
        size = image.size
    overlay = Image.new('RGBA', size, (0, 0, 0, 0))
    render_text(text, overlay)
    with tempfile.TemporaryDirectory() as tmp:
        overlay_file = Path(tmp) / 'overlay.png'
        overlay.save(overlay_file)
        result_file = Path(tmp) / 'result.mp4'
        subprocess.run([
            'ffmpeg',
            '-hide_banner',
            '-v', 'warning',
            '-i', str(style.video),
            '-i', str(overlay_file),
            '-filter_complex', 'overlay=x=0:y=0',
            '-c:v', 'libx264', '-preset', 'superfast', '-crf', '27', '-f', 'mp4', '-c:a', 'copy',
            '-y', str(result_file)
        ]).check_returncode()
        return result_file.read_bytes()


async def start(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    template = r"""Hi {you}!
To shitpost, type @{me} and type the text you want to overlay over crab rave.
This was originally made by @boringcactus in one afternoon when ze was bored.
This bot isn't super reliable but the source is at https://github.com/boringcactus/crabravebot,
and you can use this bot from the Web at https://{domain}/"""
    you = update.effective_user.first_name
    if you is None or len(you) == 0:
        you = update.effective_user.username
    text = template.format(you=you, me=context.bot.username, domain=os.environ.get('VIRTUAL_HOST', 'crabrave.boringcactus.com'))
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text)


async def inline_query(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query
    text = query.query

    logger.info('Got query %s', text)

    def make_result(style: Style):
        base = "https://" + os.environ.get('VIRTUAL_HOST', 'crabrave.boringcactus.com') + "/render?v=1&style=" + style.id + "&text=" + url_quote(text, safe='')
        return telegram.InlineQueryResultVideo(
            id=style.id,
            video_url=base + '&ext=mp4',
            mime_type="video/mp4",
            thumb_url=base + '&ext=png',
            title=style.name,
        )

    await query.answer([make_result(s) for s in STYLES])


async def message(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == telegram.Chat.PRIVATE:
        text = update.effective_message.text

        logger.info('Got query %s', text)

        await update.effective_message.reply_video(BytesIO(make_video(text, 'classic')))


@app.route('/')
def index():
    return render_template('index.html', styles=STYLES)


@app.route('/render')
def serve_render():
    style = request.args['style']
    ext = request.args['ext']
    text = request.args['text']
    if sum(1 for x in STYLES if x.id == style) == 0:
        raise ValueError('bad style')
    if ext == 'png':
        response = make_image(text, style)
        content_type = 'image/png'
    elif ext == 'mp4':
        response = make_video(text, style)
        content_type = 'video/mp4'
    else:
        raise ValueError('bad extension')
    return response, {'Content-Type': content_type}


TOKEN = os.environ.get('TG_BOT_TOKEN')
WEBHOOK = '/webhook/' + TOKEN


@app.route(WEBHOOK, methods=['POST'])
def webhook():
    update_queue.put(telegram.Update.de_json(request.get_json(), bot))
    return 'ok'


#TODO: I have no idea what this is, seems to work without it...
    # bot = telegram.Bot(token=TOKEN)
    # bot.set_webhook('https://' + os.environ.get('VIRTUAL_HOST', 'crabrave.boringcactus.com') + WEBHOOK)
update_queue = Queue()
dp = Application.builder().token(TOKEN).build()
# Add handlers
dp.add_handler(CommandHandler('start', start))
dp.add_handler(InlineQueryHandler(inline_query))
dp.add_handler(MessageHandler(filters.TEXT, message))
dp.run_polling()

thread = Thread(target=dp.start, name='application')
thread.start()
