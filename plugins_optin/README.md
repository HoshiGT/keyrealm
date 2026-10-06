# plugins_optin/ —— 默认不启用的插件源

这里的插件**不会**被 `sync_plugins()` 扫描，因此装了也不生效：它们只出现在
「插件管理 → 可添加（内置源）」里，用户点「添加」后才会被复制进 `plugins/<类别>/<服务>/`
并加载 —— 与从 `plugins_builtin/` 恢复被删插件是同一条路径
（见 `backend/plugin_manager.py` 的 `ADDABLE_SOURCE_DIRS` / `list_available_plugins` / `install_plugin`）。

**用途**：放**缺乏实例验证**的新插件。它们照现有类型与服务仿写，但没人拿真凭据跑过，
端点是照官方文档写的、判定阈值是照同类插件推的，因此不随包上架。
等用真凭据验过（含阴性对照：乱写的密钥必须判 `invalid`）再由人工把目录**挪进** `plugins/`，
那时才成为「默认启用项」。

## 约定

- **只放已存在类别下的服务目录**（`api/ cloud/ dev/ dns/ exchange/ im/ llm/ mail/`），
  不要在这里新开类别。原因见 `docs/PLUGIN_GUIDE.md`：新类别要连带克隆一份 `<类别>_common.py`。
- **这里不放 `<类别>_common.py`**。类别共享模块仍由 `plugins/<类别>/` 提供 —— 插件目录被
  「添加」进 `plugins/` 之后，`from api_common import ...` 才由同层的 `plugins/api/` 解析得到。
  （直接就地加载 `plugins_optin/<类别>/<服务>/` 会 ImportError，这是**预期**的：它本就不该就地跑。）
- 每个服务目录与 `plugins/` 下的完全同构：`manifest.json` + `plugin.py`。
- `plugin.py` 的模块 docstring 里**必须带官方参考链接**（`参考文档：https://...`，可多行；
  位置不限——紧跟首段摘要之后或放在 docstring 末尾都行）——
  这里放的本就是「照官方文档写的、但没实例验证」的插件，没有这条链接后人无从复核。
  **可以没有，不能乱写**：确认不了官方文档地址的就不写，也不许拿博客/中转仓库页凑数。
  **只给开发者看，不上界面**：只写在 docstring 里，不许进 `manifest.json` 的 description /
  字段提示，也不许出现在 `verify()`、`get_display_summary()` 的返回里。见 `docs/PLUGIN_GUIDE.md` §3。
- `category.json` 是 `plugins/<类别>/category.json` 的副本，只为了让「可添加」卡片显示
  正确的类别名与图标；`install_plugin()` 只在目标类别目录还没有 `category.json` 时才会用到它。

## 与发行包的关系

`scripts/build.py` 会把本目录整体复制到 `release/plugins_optin/`。
`CredentialVault.spec` 的**标准库 hiddenimports 扫描同时覆盖这里** —— 插件是在运行时才从
此处复制进 `plugins/` 的，exe 构建时若漏扫，用户装完一点就 `ModuleNotFoundError`。
