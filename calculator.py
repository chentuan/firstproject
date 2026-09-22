#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一个纯标准库实现的桌面计算器。

用法:
    python3 calculator.py             启动图形界面
    python3 calculator.py --selftest  只跑求值器自检，不开窗口
    python3 calculator.py --smoke     构建一次窗口后立即退出，用于验证 GUI 能正常初始化

两个实现说明:
1. 求值部分是自己写的递归下降解析器，完全不碰 eval()，因此输入什么都不会
   被执行成代码。支持 + - * / 、括号、一元正负号和后缀百分号。
2. 按钮是自绘控件（Frame + Label）而不是 tk.Button —— macOS 的 aqua 主题会
   忽略 tk.Button 的 bg，自绘才能保证深色配色在三个平台上表现一致。
"""

from __future__ import annotations

import math
import re
import sys
import tkinter as tk
from tkinter import font as tkfont

# --------------------------------------------------------------------------- #
# 表达式求值
# --------------------------------------------------------------------------- #

MAX_INPUT_LEN = 30


class CalculationError(Exception):
    """表达式无法计算时抛出，消息直接展示给用户。"""


# number 组整体包含指数部分，避免 "1e+16" 被拆成 1 / e / +16
_TOKEN_RE = re.compile(
    r"""
      (?P<number> (?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)? )
    | (?P<op>     [+\-*/%()] )
    | (?P<space>  \s+ )
    """,
    re.VERBOSE,
)


def tokenize(text: str) -> list[tuple[str, str]]:
    """把表达式切成 (类型, 原文) 列表，跳过空白。"""
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if match is None:
            raise CalculationError(f"看不懂的符号：{text[pos]}")
        pos = match.end()
        if match.lastgroup == "space":
            continue
        kind = "number" if match.lastgroup == "number" else "op"
        tokens.append((kind, match.group()))
    return tokens


class _Parser:
    """递归下降解析器，文法如下：

        expr    := term (('+' | '-') term)*
        term    := unary (('*' | '/') unary)*
        unary   := ('+' | '-') unary | postfix
        postfix := primary '%'*
        primary := NUMBER | '(' expr ')'
    """

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self._tokens = tokens
        self._pos = 0

    def parse(self) -> float:
        value = self._expr()
        leftover = self._peek()
        if leftover is not None:
            raise CalculationError(f"多余的内容：{leftover[1]}")
        return value

    def _peek(self) -> tuple[str, str] | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _next(self, expected: str | None = None) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise CalculationError("表达式不完整")
        if expected is not None and token[1] != expected:
            raise CalculationError(f"这里应该是 {expected}，实际是 {token[1]}")
        self._pos += 1
        return token

    def _expr(self) -> float:
        value = self._term()
        while (token := self._peek()) and token[1] in "+-":
            self._next()
            rhs = self._term()
            value = value + rhs if token[1] == "+" else value - rhs
        return value

    def _term(self) -> float:
        value = self._unary()
        while (token := self._peek()) and token[1] in "*/":
            self._next()
            rhs = self._unary()
            if token[1] == "*":
                value *= rhs
            elif rhs == 0:
                raise CalculationError("不能除以零")
            else:
                value /= rhs
        return value

    def _unary(self) -> float:
        token = self._peek()
        if token and token[1] in "+-":
            self._next()
            value = self._unary()
            return -value if token[1] == "-" else value
        return self._postfix()

    def _postfix(self) -> float:
        value = self._primary()
        while (token := self._peek()) and token[1] == "%":
            self._next()
            value /= 100
        return value

    def _primary(self) -> float:
        token = self._peek()
        if token is None:
            raise CalculationError("表达式不完整")
        kind, text = token
        if kind == "number":
            self._next()
            return float(text)
        if text == "(":
            self._next("(")
            value = self._expr()
            self._next(")")
            return value
        raise CalculationError(f"位置不对的符号：{text}")


def evaluate(expression: str) -> float:
    """计算表达式并返回浮点结果，出错时抛 CalculationError。"""
    tokens = tokenize(expression)
    if not tokens:
        raise CalculationError("表达式为空")
    return _Parser(tokens).parse()


def format_number(value: float) -> str:
    """把结果格式化成适合显示的字符串，去掉浮点尾巴。"""
    if math.isnan(value) or math.isinf(value):
        raise CalculationError("结果超出可表示范围")
    if value == int(value) and abs(value) < 1e16:
        return str(int(value))
    return f"{value:.12g}"


# --------------------------------------------------------------------------- #
# 配色与字体
# --------------------------------------------------------------------------- #

BACKGROUND = "#1c1c1e"
SUBTEXT = "#8e8e93"
MAINTEXT = "#ffffff"

PALETTE_NUMBER = {"bg": "#2c2c2e", "hover": "#3a3a3c", "active": "#48484a", "fg": "#ffffff"}
PALETTE_FUNC = {"bg": "#3a3a3c", "hover": "#4a4a4c", "active": "#5a5a5c", "fg": "#ffffff"}
PALETTE_OP = {"bg": "#ff9f0a", "hover": "#ffb340", "active": "#ffc966", "fg": "#ffffff"}

PREFERRED_FONTS = ("SF Pro Display", "Helvetica Neue", "Segoe UI", "DejaVu Sans", "Arial")


def pick_font(root: tk.Misc) -> str:
    available = set(tkfont.families(root))
    for name in PREFERRED_FONTS:
        if name in available:
            return name
    return tkfont.nametofont("TkDefaultFont").actual("family")


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #

class Calculator(tk.Tk):
    """计算器主窗口。"""

    # (显示文字, 内部键值, 配色)
    LAYOUT = (
        (("AC", "ac", PALETTE_FUNC), ("⌫", "back", PALETTE_FUNC),
         ("%", "%", PALETTE_FUNC), ("÷", "/", PALETTE_OP)),
        (("7", "7", PALETTE_NUMBER), ("8", "8", PALETTE_NUMBER),
         ("9", "9", PALETTE_NUMBER), ("×", "*", PALETTE_OP)),
        (("4", "4", PALETTE_NUMBER), ("5", "5", PALETTE_NUMBER),
         ("6", "6", PALETTE_NUMBER), ("−", "-", PALETTE_OP)),
        (("1", "1", PALETTE_NUMBER), ("2", "2", PALETTE_NUMBER),
         ("3", "3", PALETTE_NUMBER), ("+", "+", PALETTE_OP)),
        (("±", "sign", PALETTE_NUMBER), ("0", "0", PALETTE_NUMBER),
         (".", ".", PALETTE_NUMBER), ("=", "=", PALETTE_OP)),
    )

    OPERATORS = "+-*/"
    # 尾部数字，用于 ± 取反
    _TAIL_NUMBER_RE = re.compile(r"(\d*\.?\d+)$")

    def __init__(self) -> None:
        super().__init__()

        self.title("计算器")
        self.configure(bg=BACKGROUND)
        self.resizable(False, False)

        self._ui_font = pick_font(self)
        self._mono_font = self._ui_font

        # 状态：expression 保存正在拼的表达式，phase 标记当前处于哪个阶段
        self._expression = ""
        self._phase = "input"          # input | result | error
        self._sub_text = ""
        self._main_text = "0"

        self._build_display()
        self._build_buttons()
        self._bind_keys()

        self._center_window(360, 560)

    # ---------------------------------------------------------------- 构建 --

    def _build_display(self) -> None:
        display = tk.Frame(self, bg=BACKGROUND)
        display.pack(fill="x", padx=22, pady=(26, 10))

        self._sub_var = tk.StringVar(value="")
        self._main_var = tk.StringVar(value="0")

        tk.Label(
            display, textvariable=self._sub_var, bg=BACKGROUND, fg=SUBTEXT,
            font=(self._ui_font, 14), anchor="e", justify="right",
        ).pack(fill="x")

        tk.Label(
            display, textvariable=self._main_var, bg=BACKGROUND, fg=MAINTEXT,
            font=(self._ui_font, 34), anchor="e", justify="right",
        ).pack(fill="x", pady=(6, 0))

    def _build_buttons(self) -> None:
        board = tk.Frame(self, bg=BACKGROUND)
        board.pack(fill="both", expand=True, padx=18, pady=(4, 10))

        for index in range(4):
            board.columnconfigure(index, weight=1, uniform="key")
        for index in range(len(self.LAYOUT)):
            board.rowconfigure(index, weight=1, uniform="key")

        for row, entries in enumerate(self.LAYOUT):
            for column, (label, key, palette) in enumerate(entries):
                # 功能键文字小一点，避免 AC / ⌫ 显得笨重
                size = 22 if key.isdigit() or key == "." else 17
                self._make_button(board, label, key, row, column, palette, size)

        tk.Label(
            self, text="键盘可直接输入 · Enter 计算 · Esc 清空",
            bg=BACKGROUND, fg="#5a5a5e", font=(self._ui_font, 11),
        ).pack(pady=(0, 14))

    def _make_button(self, parent, label, key, row, column, palette, size) -> tk.Frame:
        """自绘按钮：tk.Button 在 macOS 上不认 bg，只能自己拼。"""
        frame = tk.Frame(parent, bg=palette["bg"], cursor="hand2", highlightthickness=0)
        frame.grid(row=row, column=column, sticky="nsew", padx=5, pady=5)

        text = tk.Label(
            frame, text=label, bg=palette["bg"], fg=palette["fg"],
            font=(self._ui_font, size),
        )
        text.pack(expand=True, fill="both")

        def paint(color: str) -> None:
            frame.configure(bg=color)
            text.configure(bg=color)

        def on_press(_event: tk.Event) -> None:
            paint(palette["active"])

        def on_release(event: tk.Event) -> None:
            under = frame.winfo_containing(event.x_root, event.y_root)
            inside = under in (frame, text)
            paint(palette["hover"] if inside else palette["bg"])
            if inside:
                self._press(key)

        def on_enter(_event: tk.Event) -> None:
            paint(palette["hover"])

        def on_leave(_event: tk.Event) -> None:
            paint(palette["bg"])

        for widget in (frame, text):
            widget.bind("<Button-1>", on_press)
            widget.bind("<ButtonRelease-1>", on_release)
            widget.bind("<Enter>", on_enter)
            widget.bind("<Leave>", on_leave)

        return frame

    def _bind_keys(self) -> None:
        self.bind("<Key>", self._on_key)

    def _center_window(self, width: int, height: int) -> None:
        x = (self.winfo_screenwidth() - width) // 2
        y = (self.winfo_screenheight() - height) // 3
        self.geometry(f"{width}x{height}+{x}+{y}")

    # ---------------------------------------------------------------- 交互 --

    def _on_key(self, event: tk.Event) -> str | None:
        char = event.char
        if char in "0123456789.+-*/()%":
            self._input(char)
        elif event.keysym in ("Return", "KP_Enter", "equal"):
            self._calculate()
        elif event.keysym == "BackSpace":
            self._backspace()
        elif event.keysym in ("Escape", "Delete"):
            self._clear()
        else:
            return None
        return "break"

    def _press(self, key: str) -> None:
        if key == "=":
            self._calculate()
        elif key == "ac":
            self._clear()
        elif key == "back":
            self._backspace()
        elif key == "sign":
            self._toggle_sign()
        else:
            self._input(key)

    def _input(self, key: str) -> None:
        """追加一个字符，处理连续运算符、重复小数点等边界情况。"""
        if self._phase == "error":
            self._clear()

        if self._phase == "result":
            # 结果之后按数字就重新开始，按运算符则接着算
            if key not in self.OPERATORS:
                self._expression = ""
            self._phase = "input"

        if key in self.OPERATORS:
            if self._expression and self._expression[-1] in self.OPERATORS:
                # 连着按两个运算符：用新的替换旧的（"5*-" 这种一元负号保留）
                if self._expression[-1] != "-":
                    self._expression = self._expression[:-1]
            elif not self._expression and key != "-":
                return
        elif key == ".":
            if not self._expression or self._expression[-1] in self.OPERATORS + "(":
                key = "0."
            else:
                current = re.split(r"[+\-*/(]", self._expression)[-1]
                if "." in current:
                    return

        if len(self._expression) >= MAX_INPUT_LEN:
            return

        self._expression += key
        self._sub_text = ""
        self._main_text = self._expression
        self._refresh()

    def _backspace(self) -> None:
        if self._phase == "result":
            self._clear()
            return
        if self._phase == "error":
            self._clear()
            return
        self._expression = self._expression[:-1]
        self._main_text = self._expression or "0"
        self._refresh()

    def _clear(self) -> None:
        self._expression = ""
        self._phase = "input"
        self._sub_text = ""
        self._main_text = "0"
        self._refresh()

    def _toggle_sign(self) -> None:
        """给表达式末尾的数字取反，例如 5-3 → 5--3。"""
        if self._phase == "error":
            self._clear()
            return
        if self._phase == "result":
            self._expression = f"-({self._expression})"
            self._phase = "input"
            self._main_text = self._expression
            self._refresh()
            return

        match = self._TAIL_NUMBER_RE.search(self._expression)
        if match is None:
            return

        start = match.start()
        before = self._expression[start - 2] if start >= 2 else ""
        if start > 0 and self._expression[start - 1] == "-" and (before == "" or before in "(+-*/"):
            # 已有的负号是一元负号，去掉它
            self._expression = self._expression[: start - 1] + self._expression[start:]
        else:
            self._expression = self._expression[:start] + "-" + self._expression[start:]

        self._main_text = self._expression
        self._refresh()

    def _calculate(self) -> None:
        expression = self._expression.strip()
        if not expression:
            return
        try:
            result = format_number(evaluate(expression))
        except CalculationError as error:
            self._phase = "error"
            self._sub_text = str(error)
            self._main_text = "错误"
        else:
            self._expression = result
            self._phase = "result"
            self._sub_text = f"{expression} ="
            self._main_text = result
        self._refresh()

    def _refresh(self) -> None:
        self._sub_var.set(self._sub_text)
        self._main_var.set(self._main_text)


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

_SELF_TEST_CASES = (
    ("1+1", "2"),
    ("2*3+4", "10"),
    ("(1+2)*3", "9"),
    ("((1+2)*(3+4))", "21"),
    ("10/4", "2.5"),
    ("1/8", "0.125"),
    ("-5+3", "-2"),
    ("3-(-2)", "5"),
    ("2*-3", "-6"),
    ("-(-3)", "3"),
    ("2*(3+4)-8/2", "10"),
    ("50%", "0.5"),
    ("100%", "1"),
    ("200*10%", "20"),
    ("1.5*2", "3"),
    ("0.1+0.2", "0.3"),
    ("1/3", "0.333333333333"),
    ("5--3", "8"),
)

_SELF_TEST_ERRORS = ("", "1/0", "1+", "(1+2", "1.2.3", "abc", "*5", "()", "1..2", "5+*2")


def _exercise_ui(app: "Calculator") -> list[str]:
    """喂几组按键序列，验证输入状态机（不依赖真实点击）。"""
    failures: list[str] = []

    def feed(keys: list[str]) -> str:
        app._clear()
        for key in keys:
            app._press(key)
        return app._main_text

    checks = (
        (["5", "+", "3", "="], "8"),
        (["1", "/", "0", "="], "错误"),
        (["5", "sign"], "-5"),
        (["1", "+", "+"], "1+"),
        (["0", ".", "5", "*", "4", "="], "2"),
        (["9", "back"], "0"),
        (["2", "ac"], "0"),
        (["3", "+", "4", "=", "2"], "2"),
        (["3", "+", "4", "=", "*", "2", "="], "14"),
        (["1", ".", ".", "5"], "1.5"),
    )

    for keys, expected in checks:
        actual = feed(keys)
        if actual != expected:
            failures.append(f"按键 {' '.join(keys)}：期望 {expected!r}，实际 {actual!r}")

    return failures


def _selftest() -> int:
    failures: list[str] = []

    for expression, expected in _SELF_TEST_CASES:
        try:
            actual = format_number(evaluate(expression))
        except CalculationError as error:
            failures.append(f"{expression!r}: 意外报错 {error}")
            continue
        if actual != expected:
            failures.append(f"{expression!r}: 期望 {expected}，实际 {actual}")

    for expression in _SELF_TEST_ERRORS:
        try:
            value = evaluate(expression)
        except CalculationError:
            continue
        failures.append(f"{expression!r}: 期望报错，却算出了 {value}")

    if failures:
        print(f"自检失败 {len(failures)} 项：")
        for item in failures:
            print("  -", item)
        return 1

    total = len(_SELF_TEST_CASES) + len(_SELF_TEST_ERRORS)
    print(f"自检通过，共 {total} 项。")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()

    app = Calculator()
    if "--smoke" in argv:
        app.update_idletasks()
        app.update()

        failures = _exercise_ui(app)
        app.destroy()

        if failures:
            print(f"GUI 状态机检查失败 {len(failures)} 项：")
            for item in failures:
                print("  -", item)
            return 1
        print("GUI 构建成功，按键状态机检查通过。")
        return 0

    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
