"""Local UI messages; never changes model prompts or manuscript language."""
from __future__ import annotations

LANGUAGES = ('en', 'zh-CN')


def language(value=None):
    return value if value in LANGUAGES else 'en'


def text(locale, english, chinese, **values):
    return (chinese if language(locale) == 'zh-CN' else english).format(**values)


class LocalizedError(ValueError):
    def __init__(self, english, chinese, **values):
        self.english, self.chinese, self.values = english, chinese, values
        super().__init__(english.format(**values))

    def render(self, locale):
        return text(locale, self.english, self.chinese, **self.values)


def error_text(error, locale):
    return error.render(locale) if isinstance(error, LocalizedError) else str(error)


def status_text(value, locale):
    translated = {'preparing': '准备中', 'starting': '启动中', 'running': '运行中',
                  'planning': '规划中', 'cancelling': '正在停止', 'cancelled': '已停止',
                  'failed': '运行失败', 'completed': '已完成', 'succeeded': '已完成',
                  'interrupted': '已中断'}
    return translated.get(value, value) if language(locale) == 'zh-CN' else value
