# 界面优化记录

目标：把桌面控制台从“能用的原生窗口”收敛到 Apple HIG 的排版、圆角和动效标准，
同时保留 Windows 上实际可用的控件尺寸与键盘操作。每一轮都先看截图，再决定下一轮。

主要依据（`~/.claude/skills/apple-design/references/hig/`）：

| 文件 | 用到的规则 |
| --- | --- |
| `typography.md` | macOS 默认 13pt、最小 10pt；六档文字样式；小字避免细字重 |
| `layout.md` | 对齐与分组表达层级；按可用空间而非设备布局 |
| `sidebars.md` | 侧栏最多两级；关键操作不放底部 |
| `buttons.md` | 控件需要按压态；桌面默认 28×28 |
| `motion.md` | 动效短暂、可关闭、频繁交互不加动画 |
| `accessibility.md` | 正文对比度 4.5:1；动效尊重系统“减少动画” |

## 设计 token

字号（`TYPE`）与圆角（`RADIUS`）集中在 `src/flixcli/gui.py`，QSS 通过
`string.Template` 注入，改一处即可全局生效。

| token | 值 | 用途 |
| --- | --- | --- |
| `title2` | 17px | 页面标题、空状态标题 |
| `title3` | 15px | 会话页头（第二轮后改为 title2） |
| `body` | 13px | 正文、输入框、主值 |
| `callout` | 12px | 菜单、次值 |
| `subhead` | 11px | 说明、元数据、表头、状态栏 |
| `footnote` | 10px | 下限：标签片、圆点 |

| radius | 值 | 用途 |
| --- | --- | --- |
| `card` | 10px | 卡片、设置组、输入区 |
| `control` | 6px | 按钮、输入框、下拉 |
| `row` | 8px | 会话行、搜索框 |
| `tag` | 4px | 标签片、复选框 |
| `bubble` | 12px | 消息气泡 |
| `pill` | 999px | 进度条、滚动条、胶囊按钮 |

## 第 1 轮：排版与圆角收敛

问题：字号有 9/10.5/11/11.5/12/12.5/13/13.5/14/15 共十档，圆角 2–4px 混用，
控件只有悬停没有按压，页面之间缺少统一的间距节奏。

改动：

- 字号收敛为六档整数（10/11/12/13/15/17），正文 13px，应用字体 9 → 9.75pt。
- 圆角改为 4/6/8/10/12px 五档；进度条与滚动条改胶囊。
- 统一间距：导航 8/12、侧栏 16/12、页头 52px 高、内容区 20px、输入区 14/10。
- 所有按钮补 `:pressed`；键盘焦点环覆盖按钮、搜索框、列表、导航。
- 截图：chat、group、transfers、ADB、settings、empty、offline × 深浅色。

结果：层级清楚，不再出现 9px 与 13.5px 这类半像素字号；控件有了按下反馈。

## 第 2 轮：页头与次级控件

看第 1 轮截图发现两处：

1. 会话页头与侧栏标题都是 15px，主内容区反而没有明显的标题层级。
2. 设置页的“主题/材质”选项与复选框 12px，比同屏 13px 的属性值小一号，显得碎。

改动：

- `QLabel#sessionTitle` 15 → 17px（HIG Title 2），页头成为明确的视觉起点。
- `QLabel#signalText` / `#signalMeta` 10 → 11px，状态栏在 1.25 缩放下可读。
- `QPushButton#themeChoice`、`QCheckBox` 12 → 13px，选项与属性值同级。
- 截图：`D:\UserData\Temp\flix-type-2`（14 张）。

结果：页头、选项、状态栏三级信息都落在整数档上；浅色/深色结构一致。

## 第 3 轮：毛玻璃对比度与输入焦点

用截图实际像素采样，而不是凭肉眼估计：

| 位置 | 第 2 轮 | 第 3 轮 |
| --- | --- | --- |
| 深色侧栏次级文字 / 亮窗口上方 | 3.0:1 | **4.8:1** |
| 深色页头说明 / 亮窗口上方 | 3.5:1 | **5.8:1** |
| 浅色侧栏次级文字 / 暗窗口上方 | 3.5:1 | **4.7:1** |
| 浅色页头说明 | 4.2:1 | **5.8:1** |

改动：

- 毛玻璃加 `GLASS_TINT`：深色功能层用近黑染色，浅色用纯白染色（macOS vibrancy
  的做法）；深色侧栏/标题栏填充 72–75%，浅色侧栏 79%、标题栏 78%。
- 浅色 `muted` 从 `#6d6d6d` 调到 `#5e5e5e`，在透出的暗窗口上仍 ≥4.5:1。
- 输入框补键盘焦点环：编辑框获得焦点时给 `QFrame#composer` 设动态属性
  `focusRing`，QSS 画 1px 主题色边框。注意不能用属性名 `focus`——它与
  `QWidget` 自带的只读属性重名，`setProperty` 会静默失败。
- 截图：`D:\UserData\Temp\flix-type-3`，仓库留档见 `docs/ui/`。

结果：文字在任意桌面背景上都能读到；Tab 进输入框有明确落点。

## 动效

- 只在左侧分区切换时做 170ms 的 `OutCubic` 交叉淡入
  （`QGraphicsOpacityEffect` + `QPropertyAnimation`），结束后立即移除效果，
  避免持续重绘。
- 会话切换、消息刷新保持瞬时——HIG 明确说频繁交互不要加动画。
- 动画可关闭：设置 → 界面动效；默认值跟随 Windows
  “在 Windows 中显示动画”（`SPI_GETCLIENTAREAANIMATION`）。
- 偏好写入 `~/.relaydesk_gui.json` 的 `animations` 键，与 `theme`、`glass` 共存。

验证：动画中途 opacity≈0.27、结束后 `graphicsEffect()` 为 `None`；
关闭动效时页面直接切换。

## 复现与复测

```bash
.venv/Scripts/python.exe -I scripts/ui_shots.py D:/UserData/Temp/flix-type-4
```

- 截图脚本使用真实 Windows 平台并把窗口放在屏幕外，保证中文字体回退正确；
  静态截图必须 `window.animations = False`，否则会拍到透明度为 0 的瞬间。
- 对比度用截图实际像素的 RGB 中位数计算，不按主题色估算——毛玻璃的结果取决于
  后面的桌面内容。
- 运行时测试：`pytest`；截图只为视觉复核，不替代测试。

## 下一步（未做）

- 高对比度模式（`SPI_GETHIGHCONTRAST`）目前只影响标准材质，毛玻璃下没有专门分支。
- 窗口宽度小于 1040px 时侧栏仍固定 280px，尚未做收起。
- 菜单、提示气泡的圆角在部分 Windows 主题下由系统绘制，QSS 只能覆盖一部分。
