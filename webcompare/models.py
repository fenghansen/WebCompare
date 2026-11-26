"""【模块】请求模型 — API 入参的 Pydantic 校验

功能说明：登录、比较与分享请求的入参模型。统一禁止多余字段与
inf/NaN，把视图状态（模式、布局、差分基准、增益、曝光、伽马、
查看矩形等）的合法范围固化在类型里，分享链接存的就是这份状态。
server 模式追加分享可见性、重命名与删除三类入参。
"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt, field_validator

MAX_SAFE_INTEGER = 2**53 - 1  # JavaScript Number 的安全整数上限。


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Login(StrictModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class SSHConnectionRequest(StrictModel):
    """personal SSH 登录：连接配置可持久化，三个秘密字段仅交给进程内认证。"""

    label: str | None = Field(default=None, max_length=120)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=80)
    auth_method: Literal["auto", "password", "key"] | None = None
    key_path: str | None = Field(default=None, max_length=4096)
    password: SecretStr | None = Field(default=None, max_length=4096)
    private_key: SecretStr | None = Field(default=None, max_length=60_000)
    passphrase: SecretStr | None = Field(default=None, max_length=4096)
    host_fingerprint: str | None = Field(default=None, max_length=128)

    def connection_spec(self) -> dict:
        """只在调用 SSH 认证时展开秘密，避免模型 repr 或配置记录泄漏。"""
        return {key: value.get_secret_value() if isinstance(value, SecretStr) else value
                for key, value in self.model_dump(exclude_none=True).items()}


class Source(StrictModel):
    """一路媒体来源：服务器上的路径（目录或单文件）加可选显示名。"""

    path: str = Field(min_length=1, max_length=4096)
    label: str | None = Field(default=None, max_length=120)


class GroupRule(StrictModel):
    path: str = Field(min_length=1, max_length=4096)
    pattern: str = Field(min_length=1, max_length=200)
    names: list[str] | None = Field(default=None, min_length=1, max_length=500)


class ComparisonGroup(StrictModel):
    label: str = Field(min_length=1, max_length=120)
    rules: list[GroupRule] = Field(max_length=500)


class ComparisonRequest(StrictModel):
    """创建普通比较或按规则收集的分组比较。"""

    sources: list[Source] = Field(default_factory=list, max_length=500)
    combine: Literal["concat", "interleave"] = "concat"
    recursive: bool = True
    media_kind: Literal["image", "video"] = "image"
    groups: list[ComparisonGroup] | None = Field(default=None, max_length=32)
    image_order: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        default_factory=list, max_length=100000,
        description="personal 普通清单的绝对路径顺序；只重排已选文件，失效路径忽略，新文件追加")


class ReorderRequest(StrictModel):
    """重排比较的图片顺序：必须与现有清单同集合（仅顺序不同）。"""

    images: list[str] = Field(min_length=1, max_length=100000)


class GroupOrderRequest(StrictModel):
    order: list[StrictInt] = Field(max_length=32)


class ViewRect(StrictModel):
    x: float = Field(ge=-100000, le=100000)
    y: float = Field(ge=-100000, le=100000)
    width: float = Field(gt=0, le=100000)
    height: float = Field(gt=0, le=100000)


class PanelState(StrictModel):
    view: ViewRect | None = None
    rotation: Literal[0, 1, 2, 3] = 0
    flip_x: bool = False
    flip_y: bool = False
    stretch: float = Field(default=1, ge=0.0001, le=10000)


class RawOptions(StrictModel):
    """RAW layout overrides and display-only tone; null/auto uses metadata."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)

    bayer: Literal["auto", "RGGB", "BGGR", "GRBG", "GBRG"] = "auto"
    packing: Literal["auto", "ax_lsb10", "mipi_raw10", "uint16le", "float32le"] = "auto"
    width: Annotated[StrictInt, Field(ge=2, le=100000)] | None = None
    height: Annotated[StrictInt, Field(ge=2, le=100000)] | None = None
    stride_pixels: Annotated[StrictInt, Field(ge=2, le=100000)] | None = None
    bit_depth: Annotated[StrictInt, Field(ge=1, le=16)] | None = None
    black_level: float | None = Field(default=None, ge=-1e9, le=1e9, strict=True)
    black_levels: tuple[Annotated[float, Field(ge=-1e9, le=1e9, strict=True)],
                        Annotated[float, Field(ge=-1e9, le=1e9, strict=True)],
                        Annotated[float, Field(ge=-1e9, le=1e9, strict=True)],
                        Annotated[float, Field(ge=-1e9, le=1e9, strict=True)]] | None = None
    white_level: float | None = Field(default=None, ge=-1e9, le=1e9, strict=True)
    gain: float = Field(default=1, gt=0, le=4096, strict=True)
    gamma: float = Field(default=2.2, ge=0.1, le=8, strict=True)
    white_balance: tuple[Annotated[float, Field(gt=0, le=64, strict=True)],
                         Annotated[float, Field(gt=0, le=64, strict=True)],
                         Annotated[float, Field(gt=0, le=64, strict=True)]] | None = None


class ViewState(StrictModel):
    """分享链接中保存的完整视图状态：页码、模式、布局与查看矩形。"""

    version: Literal[1] = 1
    group_index: int = Field(default=0, ge=0)
    mode: Literal["split", "wipe", "diff"] = "split"
    direction: Literal[1, -1] = 1
    diff_base: int = Field(default=0, ge=0, le=63)
    opacity: float = Field(default=0.5, ge=0, le=1)
    wipe: float = Field(default=0.5, ge=0, le=1)
    gain: float = Field(default=4, ge=1, le=128)
    channel: Literal["rgb", "r", "g", "b"] = "rgb"
    exposure: float = Field(default=0, ge=-8, le=8)
    gamma: float = Field(default=1, ge=0.1, le=4)
    brightness_ref: StrictInt | None = Field(default=None, ge=0, le=63)
    layout: str = Field(default="1x2", pattern=r"^[1-9][0-9]?x[1-9][0-9]?$")
    transposed: bool = False
    grouping: Literal["aligned", "within"] = "aligned"
    natural_sort: bool = False
    quality: Literal["auto", "preview", "high", "exact"] = "auto"
    raw: RawOptions = Field(default_factory=RawOptions)
    view: ViewRect | None = None
    panels: list[PanelState] = Field(default_factory=list, max_length=64)
    video_frame: int = Field(default=0, ge=0, le=MAX_SAFE_INTEGER)
    video_offsets: dict[str, Annotated[StrictInt, Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)]] = Field(
        default_factory=dict, max_length=500
    )

    @field_validator("layout")
    @classmethod
    def bounded_layout(cls, value: str) -> str:
        """允许多位整数布局，同时把实际显示面板限制在 64 格之内。"""
        rows, columns = map(int, value.split("x"))
        if rows > 64 or columns > 64 or rows * columns > 64:
            raise ValueError("布局每边需为 1 到 64 的整数，总格数不能超过 64")
        return value


class PageView(StrictModel):
    """一页的公共查看区域与逐格显示参数，坐标使用原图像素。"""

    view: ViewRect | None = None
    panels: list[PanelState] = Field(default_factory=list, max_length=64)


class ComparisonTemplate(StrictModel):
    """personal 模式可复用的路径选图配置。"""

    version: Literal[1] = 1
    comparison: ComparisonRequest
    state: ViewState = Field(default_factory=ViewState)
    page_views: dict[Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,5})$")], PageView] = Field(default_factory=dict, max_length=500)


class ShareRequest(StrictModel):
    """创建分享：比较 ID、视图状态与可见性（默认仅自己可见）。"""

    comparison_id: str = Field(min_length=1, max_length=80)
    state: ViewState
    visibility: Literal["personal", "internal", "public"] = "personal"
    page_views: dict[Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,5})$")], PageView] = Field(default_factory=dict, max_length=500)
    selection_mode: Literal["fixed", "editable"] = "fixed"


class SharePatch(StrictModel):
    """修改既有分享的可见性（按角色限制可切换的目标档位在视图层判定）。"""

    visibility: Literal["personal", "internal", "public"]


class RenameRequest(StrictModel):
    """重命名或移动：源与目标都是虚拟路径（server 模式）或绝对路径（personal 模式）。"""

    path: str = Field(min_length=1, max_length=4096)
    new_path: str = Field(min_length=1, max_length=4096)


class DeleteRequest(StrictModel):
    """删除一个文件或目录：路径为虚拟路径（server 模式）或绝对路径（personal 模式）。"""

    path: str = Field(min_length=1, max_length=4096)


class FileDownloadRequest(StrictModel):
    """文件选择器下载请求：仅接受媒体路径与受限的目录展开选项。"""

    paths: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(min_length=1, max_length=500)
    recursive: bool = True
    media_kind: Literal["image", "video"] = "image"


class MkdirRequest(StrictModel):
    path: str = Field(min_length=1, max_length=4096)


class FileCopyRequest(StrictModel):
    """文件区粘贴请求：仅粘贴时提交路径快照，源保持只读，目标必须可写。"""

    paths: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(min_length=1, max_length=500)
    destination: str = Field(min_length=1, max_length=4096)
    recursive: bool = True
    media_kind: Literal["image", "video"] = "image"


class FileMoveRequest(StrictModel):
    """把选中的本机根项目整体移动到已有目录，保留原名称。"""

    paths: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(min_length=1, max_length=500)
    destination: str = Field(min_length=1, max_length=4096)
