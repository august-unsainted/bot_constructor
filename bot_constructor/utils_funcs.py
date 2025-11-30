import sys
import validators

from pathlib import Path
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton


def create_input_file(path: Path | str) -> FSInputFile:
    path = find_resource_path(path)
    photo = FSInputFile(path=path)
    return photo


def find_resource_path(relative_path) -> str:
    try:
        base_path = Path(sys._MEIPASS)
    except AttributeError:
        base_path = Path.cwd()
    return str(base_path / relative_path)
