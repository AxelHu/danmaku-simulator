# 网页弹幕客户端

这个 userscript 给当前网页的 HTML5 视频添加一层弹幕。你可以选本地 JSON / B站 XML，也可以读取自己指定的远端轨道或目录。客户端只负责观看，不调用模型，不上传本地文件，也不向视频网站发布评论。

首版面向桌面 Firefox / Chromium 和 Tampermonkey / Violentmonkey。播放器仍由网站控制，原站弹幕也会保留。

## 安装与第一次使用

1. 在浏览器中安装 [Tampermonkey](https://www.tampermonkey.net/) 或 [Violentmonkey](https://violentmonkey.github.io/)。
2. 打开 [安装 userscript](https://raw.githubusercontent.com/AxelHu/danmaku-simulator/main/clients/browser/danmaku-simulator.user.js)，让脚本管理器确认安装。如果浏览器只显示源码，在管理器中创建新脚本并粘贴 [完整源码](./danmaku-simulator.user.js)。
3. 打开有 HTML5 视频的网页，点击右下角“弹幕”。默认选择当前可见面积最大的已发现视频；多播放器页面可以在面板顶部切换。
4. 点击“打开本地 JSON / XML”，或者粘贴来源 URL 后点击“读取”。目录读入后还需要选择一条轨道并点击“载入所选轨道”。
5. 用网站原有播放器播放、暂停、拖动进度或调速。附加弹幕会跟随视频时间。面板可调偏移、时间倍率、密度、字号、透明度和显示范围。

页面没有出现按钮时，可以从脚本管理器菜单选择“打开弹幕模拟器”，或用面板的刷新按钮重新寻找视频。设置和导入内容只保留在当前页面会话，刷新页面后需要重新选择来源。

安装脚本与访问来源使用脚本管理器自己的权限提示。本项目不会替你安装到日常浏览器或更改其账户设置。

## 三种来源

| 来源 | 操作 | 保留的信息 |
| --- | --- | --- |
| 本地文件 | 选择本项目的 `comments.json`、发布后的 track JSON，或 B站 XML | JSON 可保留合成标签、绑定和时间映射；本地文件不上传 |
| 轨道直链 | 粘贴直接返回 JSON / XML 的 HTTP(S) URL | 按文件元数据解析；网页地址或视频地址本身不是弹幕文件 |
| 目录 | 粘贴 `danmaku-catalog/v1` URL，再显式选择一条轨道 | 解析相对 `href`，优先 JSON，提供时核对文件字节数与 SHA-256 |

可用的公开示例目录：

```text
https://raw.githubusercontent.com/AxelHu/danmaku-simulator/main/examples/catalog/catalog.json
```

本机运行项目服务时，也可以读取：

```text
http://127.0.0.1:8793/catalog/catalog.json
```

`127.0.0.1` 指运行浏览器的电脑。服务在另一台电脑时，使用你自己可访问的服务器地址。目录只包含你显式发布的轨道；客户端不会扫描页面链接、自动搜索网络，或下载目录内的全部内容。

目录的 SHA-256 用于核对轨道是否与目录记录一致，不代表对来源身份的独立认证。来源 URL 只显示到主机与路径，面板不展示查询参数。

## 时间轴与视频匹配

JSON 轨道的时间计算为：

```text
视频时间 = 弹幕原时间 × timeline.scale + timeline.offset
```

例如弹幕原本在第 10 秒出现，`scale=1`、`offset=1.5` 时会在第 11.5 秒出现；正偏移表示延后。想修正片头长短，先调偏移；只有整段视频存在统一速度差异时，再调整倍率。这不支持把任意重剪、删段、插入广告后的版本自动对齐。

`timeline.target_duration` 可指定目标版本时长。没有该字段时，有原视频时长的 JSON 使用 `video.duration × scale + offset`。映射后小于 0 或达到目标时长的条目不参与显示，也不占用轨道。XML 不包含这些 JSON 元数据；本项目发布的 XML 已应用时间映射，导入时默认从倍率 1、偏移 0 开始。

匹配行为如下：

- 没有 `bindings`：视为用户手动选轨；会检查已知视频时长。
- B站绑定：核对可见 URL 的 BV 号和 `p` 分P；`cid` 仅从 URL 或播放器附近公开的 `data-cid` 属性读取，双方都能确认时才进一步核对。明确的 BV / 分P / cid 不一致会停用，不能靠标题相似自动通过。
- 通用网页绑定：规范化 HTTP(S) URL，排序查询参数，忽略 `utm_*`、`spm_id_from`、`share_source`，保留可能表示分集的 fragment。
- 无法识别的绑定：先停用，用户确认内容后可以点击“匹配当前视频”。明确的绑定冲突需要更换对应视频或轨道。
- 已知视频时长与轨道预计时长相差超过 `max(2 秒, 预计时长的 1%)`：显示时长差并停止自动匹配。可以调整时间轴后匹配，或者在确认版本后手动匹配。

切换播放器、替换媒体源、改变可识别的页面或分P后，旧轨道会停用并提示重新匹配。读取远端文件期间换集，也不会把迟到的响应自动套到新视频。

JSON 标明合成来源时，面板和画面角落会显示“合成弹幕”。普通 XML 缺少这项元数据，会标为“导入弹幕”；这不代表它来自真实用户。

## 全屏与显示

支持普通滚动弹幕（模式 1）、顶部固定（5）、底部固定（4）。显示范围控制可用区域，底部固定行位于该区域的底部。重复文本保留；密度抽样和避碰只改变当前显示，不改源文件。暂不渲染高级定位、脚本、逆向等特殊模式，面板会提示略过条数。

客户端用媒体 `currentTime` 计算位置，暂停时画面固定，seek 后重建当前时刻的弹幕，倍速跟随原播放器。滚出视口、隐藏页面或隐藏视频时停止持续绘制，恢复可见后继续。

网站把播放器容器全屏化时，客户端只把自己的叠加层放入全屏容器；原生 `<video>` 全屏使用浏览器的 Popover top layer。面板也提供“带弹幕全屏”按钮。旧浏览器不支持 Popover 时，容器全屏是可用的回退，原生视频全屏可能显示不了附加层。

画布按视频元素实际位置、`object-fit` 和 letterbox 留白定位。视频文件内已经编码的黑边无法自动识别；此版本也没有人物遮挡蒙版。

## 权限与适用边界

`@match http://*/*` 和 `https://*/*` 用于发现不同网站的视频。`@connect *` 用于用户显式输入的远端来源，脚本在启动、发现视频或换集时不会自动发出源请求。远端读取只使用 GET，优先采用 `GM_xmlhttpRequest` / `GM.xmlHttpRequest`，并设置 `anonymous: true`；不读取网站账户、cookie、token 或登录状态。

没有 GM 请求接口时，使用 `fetch`，并设置 `credentials: 'omit'`。普通 fetch 仍受 CORS、混合内容及浏览器本地网络访问规则约束。脚本管理器读取失败后不会悄悄改用另一种请求方式重试；面板会展示错误。HTTPS 视频页读取本地 HTTP 服务，应使用具备 GM 请求权限的脚本管理器，并按管理器自己的策略允许该来源。

UI 采用闭合 Shadow DOM 以隔离样式和控件，外来文本通过 `textContent` 或 Canvas 绘制。Shadow DOM 不是对恶意网页的安全隔离边界。轨道应当是可用于当前网页观看的内容，不应携带凭据。

当前明确的限制：

- 只处理顶层页面可访问的 `<video>`。脚本使用 `@noframes`，不跨 iframe 读取播放器；只有 canvas、封闭 Shadow DOM 或专用插件的播放器也可能无法识别。
- 开放 Shadow DOM 会在发现后增量监听；网站在已存在元素上后来才创建的 ShadowRoot，可能需要点击刷新重新发现。
- 如果网站复用同一个视频元素、媒体源和页面 URL，且换集后没有可辨识的变化，通用客户端不能保证识别换集。遇到这种播放器，请重新选择对应轨道；后续可以加入站点适配。
- 画中画窗口不属于网页 DOM，不能携带此 Canvas 层。移动端和各视频网站的专有全屏模式没有普遍兼容承诺。
- 当前客户端单文件上限为 32 MiB。所有弹幕在本页内存中解析，超大轨道应先离线整理。

## 验证记录

本轮使用独立、临时的 Firefox 151.0 与 Chromium 149.0 浏览器；没有打开或安装到日常 Firefox。HTML5 测试页面播放真实的 52.208 秒 Sintel 预告片，读取本项目实际发布的 65 条标准轨道。Sintel 由 Blender Foundation 制作，按 [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/) 发布，项目主页为 [sintel.org](https://www.sintel.org/)。

| 检查 | 结果与证据范围 |
| --- | --- |
| 两个浏览器的媒体与客户端行为 | 各 28 项通过，页面脚本错误均为 0 |
| 本地 JSON / XML，远端直链，目录选轨 | 真实文件、真实 HTTP 响应与 SHA-256 一致性通过；目录不会自动下载轨道 |
| 暂停、seek、2×、开关、定位 | 媒体时间与 Canvas 像素检查通过 |
| 原生视频全屏、容器全屏 | 两浏览器均有实际截图；Popover 层和容器内定位通过 |
| SPA、播放器替换、多视频切换、迟到请求 | 旧轨道停用与重新匹配通过 |
| 已知时长差、绑定冲突、负时间与目标时长 | 手动确认 / 阻止错配 / 裁剪通过；纯函数测试覆盖边界 |
| 普通 fetch | 在真正的临时 HTTP 页面上跨源读取实际 catalog 轨道通过；没有关闭浏览器安全策略 |
| GM 读取 | **使用测试桥接模拟 GM 接口，传输真实 HTTP 文件；未安装真实 Tampermonkey / Violentmonkey 扩展**。Firefox 测现代 GM API，Chromium 测传统 API；HTTPS 测试页 → 本地 HTTP 也仅验证这条接口路径 |
| BFCache | `pagehide/pageshow` 的 `persisted` 事件处理与绘制恢复通过；测试模拟生命周期事件，不保证任意网站会被浏览器纳入 BFCache |
| 公开 B站页面 | 实际访问 [BV1iW411j7bn](https://www.bilibili.com/video/BV1iW411j7bn/) 得到 **HTTP 412**，有一次明确显示风控拒绝；后续可读到部分标题/简介，但 `video` 数量仍为 0。没有验证 B站实际播放器、附着层或全屏，也没有登录或尝试绕过风控 |

因此，通用 HTML5 客户端的播放行为已有验证；真实脚本扩展的权限端到端、以及具体视频网站实际播放器兼容性，仍需要在可正常播放的环境中确认。不能把本地 HTML5 夹具等同于 B站验收。

### 复现

纯函数测试不需要浏览器：

```bash
node --test tests/browser/userscript.test.cjs
```

浏览器检查需要 Python Playwright、已安装的 Firefox / Chromium、正在运行的项目服务、`sintel-v2` 视频与已发布的 `standard` 轨道：

在新克隆的仓库中可直接准备已记录样例，无需调用模型：

```bash
python3 tools/prepare_sintel_demo.py --out runs/sintel-v2
python3 -m danmaku_sim serve --port 8793 --library examples/catalog
```

保持服务运行，在另一个终端执行检查：

```bash
python3 tests/browser/qa_userscript.py --base http://127.0.0.1:8793
```

加 `--external` 才会执行一次公开 B站页的只读检查。`--chrome` 可指定 Chromium 可执行文件；否则从 PATH 寻找 Chrome / Chromium，或使用 Playwright 的版本。测试会关闭自己启动的浏览器和临时 HTTP 服务，报告与截图写入 `.local/browser-qa/`。测试用 ShadowRoot 捕获与 GM 桥接只存在于测试脚本，交付的 userscript 没有这些全局入口。

## 方案依据

- [Tampermonkey：GM_xmlhttpRequest](https://www.tampermonkey.net/documentation.php?q=GM_xmlhttpRequest)、[GM_registerMenuCommand](https://www.tampermonkey.net/documentation.php?q=GM_registerMenuCommand)、[@connect](https://www.tampermonkey.net/documentation.php?q=connect)：后台来源请求、匿名请求选项、菜单入口与目标权限。
- [Violentmonkey：GM API](https://violentmonkey.github.io/api/gm/)：传统 GM_* 与 GM.* API 兼容形式。
- [MDN：HTMLMediaElement.currentTime](https://developer.mozilla.org/en-US/docs/Web/API/HTMLMediaElement/currentTime)、[playbackRate](https://developer.mozilla.org/en-US/docs/Web/API/HTMLMediaElement/playbackRate)：以播放器时钟同步，而不是另起墙上时钟。
- [MDN：Fullscreen API](https://developer.mozilla.org/en-US/docs/Web/API/Fullscreen_API)、[showPopover](https://developer.mozilla.org/en-US/docs/Web/API/HTMLElement/showPopover)、[Top layer](https://developer.mozilla.org/en-US/docs/Glossary/Top_layer)：全屏容器与原生视频叠层的处理。
