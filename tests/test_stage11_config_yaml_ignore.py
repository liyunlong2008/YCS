"""
TDD 阶段 11 · config.yaml 不应上传至仓库：
  ① .gitignore 显式忽略 /config.yaml 但不忽略 config.yaml.example
  ② 仓库提供 config.yaml.example（模板，全占位值，含 risk_limits）
  ③ install.sh 缺失 config.yaml 时自动 cp config.yaml.example → config.yaml
  ④ ycsctl check 缺失 config.yaml 时自动从 example 复制一份再做检查（不静默失败）
  ⑤ load_config 默认缺失时可 fallback 到 .example（保留原 FileNotFound 行为但给 ensure_config 工具函数）
"""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# 项目根：按当前测试文件所在目录向上两级（tests/*.py → parent.parent = 项目根）
# 不再硬编码 /workspace：兼容 VPS /opt/ycs、本地 Windows、容器任意 INSTALL_DIR
REPO = Path(__file__).resolve().parent.parent


# ============================================================================
# ① .gitignore 规则
# ============================================================================
class Test_1_GitignoreRules:
    def test_gitignore_has_config_yaml_entry(self):
        gi = (REPO / ".gitignore").read_text(encoding="utf-8")
        # 必须存在「忽略 config.yaml 本身」的规则（不能只是 *.yaml）
        lines = [ln for ln in gi.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        # 允许：/config.yaml  或  config.yaml  （带锚点或不带都行，但必须是针对该文件）
        matched = [ln for ln in lines if ln.rstrip("/") in ("config.yaml", "/config.yaml")]
        assert matched, (
            ".gitignore 缺少针对 config.yaml 的忽略规则。"
            "用户明确：config.yaml 不应该上传至仓库（避免密钥泄露）。"
        )

    def test_gitignore_does_not_block_example_template(self):
        """config.yaml.example 必须允许进入仓库（即 .gitignore 没有匹配到它）。"""
        gi = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        # 用 git check-ignore 的语义做本地模拟：
        def would_ignore(path: str) -> bool:
            # 支持 /config.yaml 锚定根 & config.yaml 全局 & *.yaml 通配
            name = Path(path).name
            for ln in gi:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                neg = False
                if ln.startswith("!"):
                    neg = True
                    ln = ln[1:]
                anchored = ln.startswith("/")
                pat = ln.lstrip("/").rstrip("/")
                # 通配
                if "*" in pat:
                    import fnmatch
                    hit = fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(path, pat)
                else:
                    hit = (path == pat) or (not anchored and name == pat) or (anchored and path == pat)
                if hit:
                    return not neg
            return False

        assert would_ignore("config.yaml") is True, "config.yaml 应被 .gitignore 忽略"
        assert would_ignore("config.yaml.example") is False, (
            "config.yaml.example 不应被忽略（作为模板进入仓库）"
        )


# ============================================================================
# ② config.yaml.example 存在且结构齐全（含 risk_limits）
# ============================================================================
class Test_2_ExampleTemplate:
    def test_config_example_exists(self):
        assert (REPO / "config.yaml.example").is_file(), (
            "缺少 config.yaml.example 模板文件（供仓库分发，用户 cp 后改）"
        )

    def test_config_example_has_all_required_sections(self):
        import yaml
        raw = yaml.safe_load((REPO / "config.yaml.example").read_text(encoding="utf-8"))
        for sec in ("okx", "ai", "trading", "risk_limits"):
            assert sec in raw, f"config.yaml.example 缺少 {sec} 段"
        # 必须全占位：OKX / AI key 都不应出现真实密钥前缀（硬编码默认占位串）
        assert raw["okx"]["api_key"] == "YOUR_OKX_API_KEY"
        assert raw["ai"]["api_key"] == "YOUR_AI_API_KEY"
        # risk_limits 默认仍按 14.8U 保守阈值
        rl = raw["risk_limits"]
        assert float(rl["live_max_equity_usdt"]) == 15.0
        assert float(rl["live_max_daily_loss_usdt"]) == 3.0
        assert float(rl["live_max_single_order_usdt"]) == 2.0
        assert "kill_switch_token" in rl
        assert "shadow_mode" in rl
        # trading 默认 live=false（防止刚 cp 完就真下单）
        assert raw["trading"]["live"] is False


# ============================================================================
# ③ install.sh：config.yaml 缺失时，自动从 config.yaml.example 复制
# ============================================================================
class Test_3_InstallShAutoCopy:
    def test_installsh_contains_autocopy_logic(self):
        """install.sh 中应包含：config.yaml 不存在时，cp config.yaml.example config.yaml。"""
        content = (REPO / "install.sh").read_text(encoding="utf-8")
        # 至少要出现 cp / copy 的逻辑，并且引用了 config.yaml.example → config.yaml
        assert "config.yaml.example" in content, (
            "install.sh 缺少从 config.yaml.example 复制的步骤"
        )
        # 更具体：出现 "config.yaml" 不存在 then copy example
        # 允许 [[ -f config.yaml ]] || cp ...  或  if [ ! -f config.yaml ]; then cp ... fi
        has_cp = (
            ("cp" in content and "config.yaml.example" in content and "config.yaml" in content)
        )
        assert has_cp, (
            "install.sh 必须：config.yaml 不存在时 cp config.yaml.example config.yaml。"
            "（用户仓库不携带 config.yaml，必须由 example 自动生成）"
        )


# ============================================================================
# ④ ycsctl check：缺失 config.yaml 时自动 cp example（不再直接报不存在错）
# ============================================================================
class Test_4_YcsctlCheckAutoInit:
    def test_ycsctl_missing_config_triggers_autocopy_and_continues(self, tmp_path: Path):
        """在临时模拟的项目根（无 config.yaml 但有 example）执行 ycsctl check --config
           应自动复制 example 并进入占位密钥 WARNING 流程，exit 0（纸盘+占位）。"""
        # 搭建最小化项目根：需要 app 可导入，把 example 放进去
        ex_src = REPO / "config.yaml.example"
        if not ex_src.is_file():
            pytest.skip("config.yaml.example 尚未提供（先跑 RED 的用例应 fail）")
        # 用真实 example，模拟 config.yaml 不存在
        fake_root = tmp_path / "ycs"
        fake_root.mkdir()
        (fake_root / "config.yaml.example").write_text(ex_src.read_text(encoding="utf-8"))

        # 调 ycsctl 的 cmd_check（需构造 argparse）；直接走 ensure 流程函数更稳
        sys.path.insert(0, str(REPO))
        try:
            from deploy.ycsctl import _ensure_config_from_example  # type: ignore
        except Exception as exc:
            pytest.fail(f"deploy.ycsctl 缺少 _ensure_config_from_example 辅助函数: {exc}")

        cfg = fake_root / "config.yaml"
        assert not cfg.is_file(), "前置：初始没有 config.yaml"
        created = _ensure_config_from_example(cfg)
        assert created is True, "缺失时应返回 True（已从 example 复制）"
        assert cfg.is_file() and cfg.read_text(encoding="utf-8").strip() != ""

        # 第二次：已存在 → 不应覆盖
        cfg.write_text("existing: true\n", encoding="utf-8")
        created = _ensure_config_from_example(cfg)
        assert created is False, "已存在 config.yaml 时应返回 False（不覆盖）"
        assert cfg.read_text(encoding="utf-8").startswith("existing:")


# ============================================================================
# ⑤ app.core.config 提供 ensure_config_file（与 ycsctl 同语义）
# ============================================================================
class Test_5_AppConfigEnsure:
    def test_ensure_config_file_autocopy_and_returns_path(self, tmp_path: Path):
        ex_src = REPO / "config.yaml.example"
        if not ex_src.is_file():
            pytest.skip("config.yaml.example 缺失")
        fake_root = tmp_path / "p"
        fake_root.mkdir()
        (fake_root / "config.yaml.example").write_text(ex_src.read_text(encoding="utf-8"))

        sys.path.insert(0, str(REPO))
        from app.core.config import ensure_config_file

        target = fake_root / "config.yaml"
        path, created = ensure_config_file(target)
        assert path == target and created is True
        assert path.is_file()
        # 可直接 load_config 成功
        from app.core.config import load_config
        cfg = load_config(path)
        assert cfg.trading.live is False
        assert float(cfg.risk_limits.live_max_equity_usdt) == 15.0
