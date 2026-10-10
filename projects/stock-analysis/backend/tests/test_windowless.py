import subprocess
import sys
import textwrap


def test_main_survives_missing_console_streams(tmp_path):
    """pythonw 相当（sys.stdout/stderr が None）でも uvicorn のロガー初期化で落ちない。"""
    code = textwrap.dedent(f"""
        import os, sys
        os.environ["STOCK_DB_PATH"] = r"{tmp_path / 'w.db'}"
        sys.stdout = None
        sys.stderr = None
        from stock_analysis.__main__ import ensure_console_streams
        ensure_console_streams()
        import uvicorn.logging as ul
        ul.DefaultFormatter(fmt="%(message)s")
        print("ok")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "ok" in (tmp_path / "console.log").read_text(encoding="utf-8")
