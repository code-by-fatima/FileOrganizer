import sys
from organizer import FileOrganizer


def main():
    source_folder = sys.argv[1] if len(sys.argv) > 1 else
    destination_folder = sys.argv[2] if len(sys.argv) > 2 else 

    organizer = FileOrganizer(source_folder, destination_folder)

    try:
        organizer.organize()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except OSError as e:
        print(f"Error while organizing files: {e}")
        sys.exit(1)

    report_text = organizer.generate_report("report.txt")

    print(report_text)
    print("\nDone! Report saved to report.txt")


if __name__ == "__main__":
    main()