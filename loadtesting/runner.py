import sys


def main():
    # Inject default locustfile if caller didn't specify one
    if "-f" not in sys.argv and "--locustfile" not in sys.argv:
        sys.argv[1:1] = ["-f", "loadtesting/locustfile.py"]

    from locust.main import main as locust_main
    locust_main()
