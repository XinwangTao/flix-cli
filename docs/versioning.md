# 版本与发布约定

## 号从哪来
- 版本号唯一来源是 `pyproject.toml` 的 `version`，`src/flixcli/__init__.py` 的 `__version__`
  必须完全一致（`tests/test_version.py` 守着）；改版本只跑
  `python scripts/bump_version.py X.Y.Z`，它会同时更新 `CHANGELOG.md`（把「未发布」落成带日期的版本）。
- 语义化：修 bug 加 PATCH，加功能加 MINOR，协议或数据不兼容加 MAJOR。
- 标签与 Release 同名：`vX.Y.Z`；`flix update` 和板子的启动自检都靠这个标签比较。

## 分支与提交
- 主分支 `main` 只接收可发布提交；功能/修复走 `feat/<主题>`、`fix/<主题>`。
- 提交信息用 Conventional Commits（`feat|fix|docs|test|chore|refactor(scope): 说明`），
  一次提交一件事，提交前跑 `pytest tests -q`。
- 未发布的功能不要提前改版本号；要发版时再 bump，避免本地版本高于线上导致自动更新空转。

## 发版
1. `python scripts/bump_version.py X.Y.Z`，把「未发布」里的内容核对一遍；
2. `pytest tests -q` 全绿；
3. 提交 `chore(release): vX.Y.Z`，打标签 `git tag -a vX.Y.Z -m "..."`；
4. 推送分支与标签，CI 出多平台产物，Release 页出现 `RelayDesk-vX.Y.Z-*`；
5. 板子/桌面端下次启动自检时会看到新版本（`FLIX_AUTO_UPDATE=1` 时自动装源码包）。
