"""登录：从登录页自己把表单读出来，然后原样提交。

这里有个能省掉一大堆代码的观察：**CSRF 防护不需要单独的策略**。

所有 CSRF 方案（Spring Security 的 ``_csrf``、Django 的 ``csrfmiddlewaretoken``、
Laravel 的 ``_token``、各种 ``authenticity_token``）在客户端看来都是同一件事
——登录页里有个 hidden input 装着一次性凭据，提交时必须原样带回。

所以与其为每种框架写一个策略类，不如**把这个表单里所有 hidden 字段连同它们
的原始值一起提交**。CSRF、nonce、时间戳、隐藏的 tenant id，全都自动覆盖。

真正需要写适配器的是另一类问题：密码在浏览器里就被 JS 加密过，服务端收到
的是密文。那种情况配置救不了，得逆出前端那段加密逻辑。
"""

from __future__ import annotations

import hashlib
import urllib.parse
from dataclasses import dataclass, field
from typing import Sequence

from .dom import Node, parse_html
from .extract import detect_client_rendered
from .session import Response, WebSession

# 猜字段名用的关键词。猜错也没关系，配置里可以显式指定。
USERNAME_HINTS = ("username", "user", "account", "loginname", "login", "email", "mobile", "phone")
PASSWORD_HINTS = ("password", "passwd", "pwd", "secret")
TOKEN_HINTS = ("csrf", "xsrf", "token", "nonce", "authenticity")

# 用户名/密码输入框可能出现的 type。
_TEXT_TYPES = ("text", "email", "tel", "number", "")


class LoginError(RuntimeError):
    """登录失败。消息直接给人看。"""


@dataclass
class LoginForm:
    """从登录页解析出来的可提交表单。"""

    action: str
    method: str
    username_field: str
    password_field: str
    hidden: dict[str, str] = field(default_factory=dict)
    token_fields: tuple[str, ...] = ()
    login_page_url: str = ""

    def payload(self, username: str, password: str) -> dict[str, str]:
        """把账号密码填进表单，hidden 字段原值保留。"""
        data = dict(self.hidden)
        data[self.username_field] = username
        data[self.password_field] = password
        return data

    def describe(self) -> str:
        bits = [f"动作 {self.method} {self.action}", f"账号字段 {self.username_field}"]
        if self.token_fields:
            bits.append("令牌字段 " + ", ".join(self.token_fields))
        else:
            bits.append("无令牌字段")
        return "；".join(bits)


def _input_name(node: Node) -> str | None:
    name = node.get("name")
    return name.strip() if name and name.strip() else None


def _guess_username(candidates: list[str], fallback: str | None = None) -> str | None:
    lowered = [(name, name.lower()) for name in candidates]
    for hint in USERNAME_HINTS:
        for name, low in lowered:
            if low == hint:
                return name
    for hint in USERNAME_HINTS:
        for name, low in lowered:
            if hint in low:
                return name
    return fallback if fallback is not None else (candidates[0] if candidates else None)


def parse_login_form(html: str, page_url: str) -> LoginForm | None:
    """在页面里找出登录表单。找不到返回 ``None``。

    判定标准只有一条：表单里有 ``type="password"`` 的输入框。这一条足以把
    登录表单和搜索框、筛选表单区分开。
    """
    tree = parse_html(html)

    for form in tree.elements("form"):
        password_field: str | None = None
        hidden: dict[str, str] = {}
        text_candidates: list[str] = []

        for node in form.walk():
            if node.tag != "input":
                continue
            name = _input_name(node)
            if name is None:
                continue
            kind = (node.get("type") or "text").lower()
            value = node.get("value") or ""

            if kind == "password":
                # 有些表单有两个密码框（登录密码 + 短信验证码），取第一个。
                if password_field is None:
                    password_field = name
            elif kind == "hidden":
                hidden[name] = value
            elif kind in ("submit", "button", "image", "reset", "file", "checkbox", "radio"):
                continue
            elif kind in _TEXT_TYPES:
                text_candidates.append(name)

        if password_field is None:
            continue

        username_field = _guess_username(text_candidates)
        if username_field is None:
            # 有密码框却没有任何文本框，多半不是登录表单，跳过。
            continue

        action = (form.get("action") or "").strip() or page_url
        method = (form.get("method") or "get").strip().upper()
        token_fields = tuple(
            name for name in hidden if any(hint in name.lower() for hint in TOKEN_HINTS)
        )

        return LoginForm(
            action=urllib.parse.urljoin(page_url, action),
            method=method,
            username_field=username_field,
            password_field=password_field,
            hidden=hidden,
            token_fields=token_fields,
            login_page_url=page_url,
        )

    return None


# 自动找登录页时依次尝试的路径。
CANDIDATE_LOGIN_PATHS = (
    "/login",
    "/admin/login",
    "/user/login",
    "/users/sign_in",
    "/signin",
    "/auth/login",
    "/login.jsp",
    "/login.html",
    "/manage/login",
)


@dataclass
class _Probe:
    """一次登录页探测的结果。

    失败时这些记录是唯一的线索：用户有权知道"到底试了哪些地址、各自什么反应"。
    只丢一句"找不到"，等于让人对着黑箱猜自己哪儿填错了。
    """

    path: str
    url: str = ""
    status: int | None = None
    error: str = ""
    size: int = 0
    digest: str = ""
    evidence: list[str] = field(default_factory=list)


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def _explain_login_failure(probes: Sequence[_Probe]) -> str:
    """把失败原因讲成一句有用的话。"""
    reachable = [probe for probe in probes if probe.status is not None]

    if not reachable:
        detail = next((probe.error for probe in probes if probe.error), "")
        return f"登录页探测失败：所有候选地址都没能访问。\n{detail}"

    shells = [probe for probe in reachable if probe.evidence]

    # 多个地址返回**完全相同**的 HTML：路由在前端，服务端对任何路径都吐同一个壳。
    same_shell = len(reachable) >= 2 and len({probe.digest for probe in reachable}) == 1

    if shells and same_shell:
        first = shells[0]
        listed = "\n".join("  · " + item for item in first.evidence)
        return (
            f"找不到登录表单：试了 {len(reachable)} 个常见入口，返回的都是同一份 HTML"
            f"（{first.size} 字节），而且是前端空壳——\n"
            f"{listed}\n\n"
            "这说明路由和渲染都发生在浏览器里（Vue / React 这类单页应用），登录表单由 "
            "JS 生成，HTML 里根本没有 <form>。\n"
            "改 --login-path 或 username_field / password_field 都没有用："
            "不是表单藏得深，是它此刻还不存在。\n\n"
            "可行路线：\n"
            "  1. 用 headless 浏览器（Playwright 等）驱动登录，等页面渲染完再把 HTML 交给 websnap；\n"
            "  2. 或者在浏览器 DevTools → Network 里找到登录请求，直接调它的接口。"
        )

    if shells:
        first = shells[0]
        listed = "\n".join("  · " + item for item in first.evidence)
        return (
            f"找不到登录表单。{first.url} 返回的页面是前端空壳，表单不在 HTML 里：\n"
            f"{listed}\n\n"
            "改 login_path 换页面也未必有用——先确认这个系统的登录页是不是由 JS 渲染的。"
        )

    lines = [f"找不到登录表单。已试过 {len(probes)} 个地址："]
    for probe in probes:
        if probe.status is None:
            lines.append(f"  GET {probe.path:<22} 打不开：{probe.error}")
        else:
            lines.append(f"  GET {probe.path:<22} {probe.status}（页面里没有 password 输入框）")
    lines.append("")
    lines.append(
        "如果登录页不在这些位置，用 --login-path 指定，"
        "或在站点配置里写死 [auth] login_path / username_field / password_field。"
    )
    return "\n".join(lines)


def locate_login_form(
    session: WebSession,
    login_path: str | None = None,
    extra_paths: tuple[str, ...] = (),
) -> tuple[LoginForm, Response]:
    """找到登录表单并返回 ``(表单, 承载它的页面响应)``。

    策略：先看 ``login_path`` 指定的页面（有就用它，找不到再退化）；没指定就
    先打根路径——很多系统根路径会 302 到登录页，跟着重定向走就到了；还不行
    才逐个试常见登录路径。
    """
    attempts: list[str] = []

    if login_path:
        attempts.append(login_path)
    attempts.append("/")
    attempts.extend(extra_paths)
    attempts.extend(CANDIDATE_LOGIN_PATHS)

    seen: set[str] = set()
    ordered: list[str] = []
    for path in attempts:
        if path not in seen:
            seen.add(path)
            ordered.append(path)

    probes: list[_Probe] = []

    for path in ordered:
        try:
            response = session.get(path)
        except Exception as error:  # 连接层错误留到最后统一解释
            probes.append(_Probe(path=path, error=str(error)))
            continue

        form = parse_login_form(response.text, response.url)
        if form is not None:
            return form, response

        # 根路径可能直接就是登录页但用 JS 渲染表单；或者根路径 302 到了
        # 别的地址，而那个地址的响应没被 HTMLParser 认出表单——都继续试。
        #
        # 记下这一笔：失败时要靠它说清"试了哪些地址、各自什么反应"。
        # digest 和 evidence 一起留着，才能识别出"所有路径返回同一个前端空壳"。
        probes.append(
            _Probe(
                path=path,
                url=response.url,
                status=response.status,
                size=len(response.text),
                digest=_digest(response.text),
                evidence=detect_client_rendered(parse_html(response.text)),
            )
        )

    raise LoginError(_explain_login_failure(probes))


def looks_logged_in(response: Response, form: LoginForm) -> tuple[bool, str]:
    """粗判这次响应说明"登录成功了"还是"还被拦在门外"。

    只做保守判断：发现明显还停留在登录页的迹象才判定失败，其余一律放行，
    由后续真正取数据的那一步去暴露问题——那里错误信息更有价值。
    """
    if response.status >= 400:
        return False, f"登录请求返回 HTTP {response.status}"

    if parse_login_form(response.text, response.url) is not None:
        return False, "提交之后页面上仍然是登录表单，账号或密码可能不对"

    final_path = urllib.parse.urlparse(response.url).path.lower()
    if final_path.rstrip("/").endswith(("login", "signin", "sign-in")):
        return False, f"被重定向回登录页（{response.url}），账号或密码可能不对"

    query = urllib.parse.urlparse(response.url).query.lower()
    if "error" in query:
        return False, f"地址里带上了错误参数（{response.url}）"

    return True, "已登录"


def verify_session(session: WebSession, form: LoginForm, probe_path: str = "/") -> None:
    """登录后再取一个页面，确认会话真的能用。

    有些系统登录接口返回 200 却什么都没给（比如把错误包在 JSON 里），光看
    登录响应判断不出来。多打一次请求能把这个坑堵死。
    """
    response = session.get(probe_path)
    if parse_login_form(response.text, response.url) is not None:
        raise LoginError(
            "登录响应看起来正常，但访问受保护页面时又被要求登录。"
            "多半是账号密码不对，或者这个系统除了 cookie 还要求额外的请求头。"
        )


def login(
    session: WebSession,
    username: str,
    password: str,
    login_path: str | None = None,
    username_field: str | None = None,
    password_field: str | None = None,
    form: LoginForm | None = None,
    probe_path: str | None = "/",
) -> LoginForm:
    """走完整个登录流程，返回实际使用的表单（方便调试与复现）。"""
    if form is None:
        form, _ = locate_login_form(session, login_path)

    # 配置可以覆盖自动猜出来的字段名。
    if username_field:
        form.username_field = username_field
    if password_field:
        form.password_field = password_field

    payload = form.payload(username, password)

    if form.method == "GET":
        query = urllib.parse.urlencode(payload)
        separator = "&" if "?" in form.action else "?"
        response = session.get(f"{form.action}{separator}{query}")
    else:
        response = session.post(form.action, payload)

    ok, reason = looks_logged_in(response, form)
    if not ok:
        raise LoginError(reason)

    if probe_path:
        verify_session(session, form, probe_path)

    return form
