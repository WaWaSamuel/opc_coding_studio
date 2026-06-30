"""EditWorkspace 单测:仓库接地 + search/replace 精确改写(M6 / F-E.7 加固)。

覆盖修复 Edit "改不出可运行代码"两个根因后的关键行为:
  - list_repo_files:只列白名单目录下真实存在的可改文件(黑名单/越界不列)
  - read_targets:读真实内容、截断、越界/不存在跳过
  - apply_search_replace:命中真实改写 / 锚点不命中 failed / 白名单拒写 skipped /
    拒绝整文件覆盖 / 同文件多处累积一次回写
"""
from __future__ import annotations

from backend.services.edit_workspace import EditWorkspace


def _seed_repo(root) -> None:
    (root / "backend").mkdir(parents=True, exist_ok=True)
    (root / "frontend" / "src").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "backend" / "a.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "frontend" / "src" / "styles.css").write_text(
        ":root {\n  --accent: #4f8cff;\n}\n", encoding="utf-8")
    (root / "data" / "secret.db").write_text("x", encoding="utf-8")
    (root / "README.md").write_text("top-level\n", encoding="utf-8")  # 不在白名单前缀


def test_list_repo_files_only_whitelisted(tmp_path):
    _seed_repo(tmp_path)
    ws = EditWorkspace(tmp_path)
    files = ws.list_repo_files()
    assert "backend/a.py" in files
    assert "frontend/src/styles.css" in files
    # 黑名单 data/ 与白名单前缀外的顶层文件都不出现
    assert not any(f.startswith("data/") for f in files)
    assert "README.md" not in files


def test_read_targets_reads_real_content_and_skips_bad(tmp_path):
    _seed_repo(tmp_path)
    ws = EditWorkspace(tmp_path)
    out = ws.read_targets([
        "frontend/src/styles.css", "nope/missing.css", "../escape.py",
    ])
    assert "frontend/src/styles.css" in out
    assert "--accent: #4f8cff" in out["frontend/src/styles.css"]
    assert "nope/missing.css" not in out
    assert "../escape.py" not in out


def test_apply_search_replace_real_edit(tmp_path):
    _seed_repo(tmp_path)
    ws = EditWorkspace(tmp_path)
    res = ws.apply_search_replace([{
        "path": "frontend/src/styles.css",
        "find": "--accent: #4f8cff",
        "replace": "--accent: #ff4fa3",
    }])
    assert res.applied.get("frontend/src/styles.css") == 1
    assert res.failed == [] and res.skipped == {}
    assert res.changed_files == ["frontend/src/styles.css"]
    text = (tmp_path / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
    assert "--accent: #ff4fa3" in text and "#4f8cff" not in text


def test_apply_search_replace_anchor_miss_failed(tmp_path):
    _seed_repo(tmp_path)
    ws = EditWorkspace(tmp_path)
    res = ws.apply_search_replace([{
        "path": "frontend/src/styles.css",
        "find": "THIS-DOES-NOT-EXIST",
        "replace": "x",
    }])
    assert res.applied == {}
    assert res.failed and res.failed[0]["path"] == "frontend/src/styles.css"
    # 文件未被触碰
    text = (tmp_path / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
    assert "#4f8cff" in text


def test_apply_search_replace_denylist_skipped(tmp_path):
    _seed_repo(tmp_path)
    ws = EditWorkspace(tmp_path)
    res = ws.apply_search_replace([{
        "path": "data/secret.db", "find": "x", "replace": "y",
    }])
    assert "data/secret.db" in res.skipped
    assert (tmp_path / "data" / "secret.db").read_text(encoding="utf-8") == "x"


def test_apply_search_replace_refuses_full_overwrite(tmp_path):
    _seed_repo(tmp_path)
    ws = EditWorkspace(tmp_path)
    # find 为空但文件已存在 → 拒绝整文件覆盖(防"改动说明当正文"重演)
    res = ws.apply_search_replace([{
        "path": "backend/a.py", "find": "", "replace": "WIPED",
    }])
    assert res.applied == {} and res.failed
    assert (tmp_path / "backend" / "a.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_apply_search_replace_create_new_file(tmp_path):
    _seed_repo(tmp_path)
    ws = EditWorkspace(tmp_path)
    # find 为空且文件不存在 → 允许新建
    res = ws.apply_search_replace([{
        "path": "backend/new_mod.py", "find": "", "replace": "VALUE = 1\n",
    }])
    assert res.applied.get("backend/new_mod.py") == 1
    assert (tmp_path / "backend" / "new_mod.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_apply_search_replace_multi_edits_same_file(tmp_path):
    _seed_repo(tmp_path)
    (tmp_path / "backend" / "multi.py").write_text(
        "A = 1\nB = 2\n", encoding="utf-8")
    ws = EditWorkspace(tmp_path)
    res = ws.apply_search_replace([
        {"path": "backend/multi.py", "find": "A = 1", "replace": "A = 10"},
        {"path": "backend/multi.py", "find": "B = 2", "replace": "B = 20"},
    ])
    assert res.applied.get("backend/multi.py") == 2
    text = (tmp_path / "backend" / "multi.py").read_text(encoding="utf-8")
    assert text == "A = 10\nB = 20\n"
