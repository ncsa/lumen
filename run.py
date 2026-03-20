from illm import create_app

app = create_app()


def main():
    app.run(debug=app.config.get("DEBUG", False))


if __name__ == "__main__":
    main()
