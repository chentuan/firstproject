"""带 cookie、能看最终地址、绝不走代理的 HTTP 会话。

三件事必须做对，否则换个系统就翻车：

1. **禁用代理。** 运行环境里常常注入了 ``HTTP_PROXY``，而抓的是内网或本机
   地址。不显式清空代理的话，一次 ``localhost`` 请求会被扔到外网代理上，
   然后得到一个莫名其妙超时——排查起来能耗掉一下午。
2. **保留 cookie。** 绝大多数后台靠 cookie 维持会话，不存就每步都要重登。
3. **看得见最终地址。** 判断"登录成功没""会话过期没"，靠的都是重定向之后
   落在哪个页面上，所以必须把 ``geturl()`` 带回来。
"""

from __future__ import annotations

import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Mapping

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 websnap/1.0"
)

_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([\w-]+)", re.I)


class RequestError(RuntimeError):
    """网络层失败。消息直接给人看，不带堆栈。"""


# macOS/Linux 上系统证书库的常见位置，按优先级排。
_SYSTEM_CA_BUNDLES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
)


def system_ca_bundle() -> str | None:
    """本机可用的 CA 证书文件；没有就返回 ``None``。``WEBSNAP_CA_BUNDLE`` 可覆盖。"""
    override = os.environ.get("WEBSNAP_CA_BUNDLE")
    if override:
        return override if Path(override).is_file() else None
    return next((path for path in _SYSTEM_CA_BUNDLES if Path(path).is_file()), None)


def build_ssl_context(verify: bool = True) -> ssl.SSLContext:
    """构造 TLS 上下文；本机缺根证书时兜底到系统证书库。

    为什么需要兜底：macOS 上用官方安装包装的 Python，ssl 只认 ``SSL_CERT_FILE``
    指向的文件，默认情况下一张根证书都没有（实测 ``get_ca_certs()`` 返回空列表）。
    此时**任何** HTTPS 站点都会报 ``CERTIFICATE_VERIFY_FAILED``——明明对方证书
    合法，报错却指向使用者，看上去像地址写错了。系统证书库就摆在那儿，直接借。
    """
    context = ssl.create_default_context()

    if not verify:
        # 内网系统用自签证书是常态，给个明确的开关，别让人去改全局环境。
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    if not context.get_ca_certs():
        bundle = system_ca_bundle()
        if bundle:
            try:
                context.load_verify_locations(cafile=bundle)
            except (OSError, ssl.SSLError):  # pragma: no cover - 证书文件损坏才会走到
                pass
    return context


def translate_network_error(url: str, error: BaseException) -> str:
    """把底层网络异常翻成一句人话。

    这层存在的理由：``urllib`` 把 DNS 失败、端口拒绝、TLS 证书不信任全都包成
    同一个 ``URLError``。一概说成"连不上，检查服务是否启动"会把排查引到错方向
    ——证书不被信任时连接明明是通的，服务也活得好好的。
    """
    reason = getattr(error, "reason", error)

    # 必须排在 SSLError 前面：它是 SSLError 的子类。
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (
            f"{url} 的 TLS 证书没通过校验。\n"
            "连接本身是通的，是本机不认这张证书——常见于自签证书、企业中间证书，"
            "或本机 Python 缺少根证书库。\n"
            "确属可信系统的话：命令行加 --insecure 跳过校验；"
            "或把系统证书库指给 Python（export SSL_CERT_FILE=/etc/ssl/cert.pem）。"
        )

    if isinstance(reason, ssl.SSLError):
        return f"{url} 的 TLS 握手失败（{reason}）。"

    if isinstance(reason, (socket.timeout, TimeoutError)):
        return f"连 {url} 超时。地址或端口可能不对，也可能被防火墙拦了。"

    if isinstance(reason, ConnectionRefusedError):
        return f"{url} 拒绝连接。那个端口没在监听——服务没起来，或者地址写错了。"

    if isinstance(reason, socket.gaierror):
        return f"域名解析不了（{url}）。地址可能拼错了，或者本机没网。"

    return f"连不上 {url}（{reason}）。"


@dataclass
class Response:
    """一次请求的结果。4xx/5xx 也照常返回，交给上层判断。"""

    url: str
    status: int
    text: str
    headers: dict[str, str]

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


def _decode(raw: bytes, headers: Mapping[str, str]) -> str:
    """按响应声明的字符集解码；没声明就在 HTML 头部找，最后兜底 UTF-8。"""
    charset = None
    content_type = ""
    try:
        content_type = headers.get("Content-Type", "") or ""
    except Exception:  # pragma: no cover - 极少数 header 对象不支持 get
        content_type = ""

    match = _CHARSET_RE.search(content_type)
    if match:
        charset = match.group(1)

    if not charset:
        head = raw[:2048].decode("ascii", errors="ignore")
        match = _CHARSET_RE.search(head)
        charset = match.group(1) if match else "utf-8"

    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class WebSession:
    """维持登录态的最小 HTTP 客户端。"""

    def __init__(
        self,
        base_url: str,
        timeout: float = 15.0,
        headers: Mapping[str, str] | None = None,
        verify: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.history: list[tuple[str, str]] = []

        self._cookie_jar = CookieJar()
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self._cookie_jar),
            # 两种模式都显式挂上：verify=False 要它跳过校验，
            # verify=True 要它带上可能兜底过的根证书库。
            urllib.request.HTTPSHandler(context=build_ssl_context(verify)),
        ]

        self._opener = urllib.request.build_opener(*handlers)
        self._headers = dict(headers or {})

    # ------------------------------------------------------------------ 头部

    def set_header(self, name: str, value: str) -> None:
        self._headers[name] = value

    def set_headers(self, headers: Mapping[str, str]) -> None:
        self._headers.update(headers)

    # ------------------------------------------------------------------ cookie

    def cookies(self) -> dict[str, str]:
        return {cookie.name: cookie.value for cookie in self._cookie_jar}

    def cookie(self, name: str) -> str | None:
        return self.cookies().get(name)

    # ------------------------------------------------------------------ 请求

    def request(
        self,
        method: str,
        path: str,
        data: Mapping[str, str] | bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        url = path if path.startswith(("http://", "https://")) else self.base_url + path

        request_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        request_headers.update(self._headers)
        request_headers.update(headers or {})

        body: bytes | None = None
        if data is not None:
            if isinstance(data, Mapping):
                body = urllib.parse.urlencode(data).encode("utf-8")
                request_headers.setdefault(
                    "Content-Type", "application/x-www-form-urlencoded; charset=UTF-8"
                )
            elif isinstance(data, (bytes, bytearray)):
                body = bytes(data)
            else:
                raise TypeError(f"data 只支持 Mapping 或 bytes，收到 {type(data).__name__}")

        request = urllib.request.Request(
            url, data=body, headers=request_headers, method=method.upper()
        )
        self.history.append((method.upper(), url))

        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                return Response(
                    url=response.geturl(),
                    status=response.status,
                    text=_decode(raw, response.headers),
                    headers={k: v for k, v in response.headers.items()},
                )
        except urllib.error.HTTPError as error:
            raw = error.read() or b""
            return Response(
                url=getattr(error, "url", None) or url,
                status=error.code,
                text=_decode(raw, error.headers or {}),
                headers={k: v for k, v in (error.headers or {}).items()},
            )
        except urllib.error.URLError as error:
            raise RequestError(translate_network_error(url, error)) from error
        except ssl.SSLError as error:  # pragma: no cover - 极少数不经 URLError 包装的情况
            raise RequestError(translate_network_error(url, error)) from error

    def get(self, path: str, headers: Mapping[str, str] | None = None) -> Response:
        return self.request("GET", path, headers=headers)

    def post(
        self,
        path: str,
        data: Mapping[str, str] | bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        return self.request("POST", path, data=data, headers=headers)

    def absolute(self, path: str) -> str:
        return urllib.parse.urljoin(self.base_url + "/", path)
