

def test_判定理由与闸门说明不含Markdown标记():
    """复核界面的「为什么没自动接受」是**纯文本**，不能带 Markdown。

    实际 `时长相同**不能**区分版本` 在界面上原样显示成星号——
    和 YAML 注释里写 `**加粗**` 是同一类错误：输出格式必须匹配载体。

    只扫描**会作为数据返回给使用者的字符串字面量**（指派给 text/detail/
    label 的值）。模块文档与注释里用 Markdown 是给人读源码的，不在此列。
    """
    import re
    from pathlib import Path

    offenders: list[str] = []
    for name in ("match.py", "api.py", "auth.py"):
        path = Path("src/lyra") / name
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            # 只看这些字段的赋值：它们是直接展示给使用者的
            if not re.search(r"\b(text|detail|label|reason)\s*=", line):
                continue
            if re.search(r"\*\*|`", line):
                offenders.append(f"{name}:{lineno}: {stripped[:70]}")

    assert not offenders, (
        "面向使用者的文案里出现了 Markdown 标记（界面会原样显示）：\n  "
        + "\n  ".join(offenders)
    )


def test_建议文案本身不含星号():
    """回归：这两句曾写作 `**不要采用**` / `**不能**`，界面上显示成星号。"""
    from pathlib import Path

    source = Path("src/lyra/match.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        # 只看真正的字符串字面量行，跳过注释与文档
        if stripped.startswith("#") or stripped.startswith('"""'):
            continue
        if ("不要采用" in line or "并不能区分版本" in line) and "**" in line:
            raise AssertionError(f"建议文案里仍有 Markdown 星号：{stripped[:80]}")
