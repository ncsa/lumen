from pathlib import Path

from lumen import create_app

app = create_app()

_DOCS_DIR = Path(__file__).resolve().parent / "docs"


def main():
    app.run(debug=app.config.get("DEBUG", False), port=5001, extra_files=[str(_DOCS_DIR / "nav.json")])


if __name__ == "__main__":
    main()
