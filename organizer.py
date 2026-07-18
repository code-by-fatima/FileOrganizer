organizer.py
import os
import shutil
from datetime import datetime

class FileOrganizer:
  CATEGORY_MAP = {
        "Images": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".tiff"],
        "Documents": [".doc", ".docx", ".txt", ".odt", ".rtf", ".html", ".htm"],
        "PDFs": [".pdf"],
        "Spreadsheets": [".xls", ".xlsx", ".csv"],
        "Presentations": [".ppt", ".pptx"],
        "Scripts": [".py", ".js", ".java", ".c", ".cpp", ".sh"],
        "Archives": [".zip", ".rar", ".7z", ".tar", ".gz"],
        "Audio": [".mp3", ".wav", ".aac"],
        "Video": [".mp4", ".mov", ".avi", ".mkv"],
    }

    OTHERS_CATEGORY = "Others"

    def __init__(self, source_folder, destination_folder):
        self.source_folder = source_folder
        self.destination_folder = destination_folder
        self.total_processed = 0
        self.category_counts = {}
        self.moved_files = []
        self.errors = []        

    def _get_category(self, file_name):
        ext = os.path.splitext(file_name)[1].lower()
        for category, extensions in self.CATEGORY_MAP.items():
            if ext in extensions:
                return category
        return self.OTHERS_CATEGORY

    def _ensure_folder(self, folder_path):
        try:
            os.makedirs(folder_path, exist_ok=True)
        except OSError as e:
            raise OSError(f"Could not create folder '{folder_path}': {e}")

    def organize(self):
        if not os.path.isdir(self.source_folder):
            raise FileNotFoundError(
                f"Source folder does not exist: {self.source_folder}"
            )

        self._ensure_folder(self.destination_folder)
        try:
            entries = os.listdir(self.source_folder)
        except OSError as e:
            raise OSError(f"Could not read source folder: {e}")

        for entry in entries:
            entry_path = os.path.join(self.source_folder, entry)

            if not os.path.isfile(entry_path):
                continue 

            self.total_processed += 1
            category = self._get_category(entry)

            try:
                category_folder = os.path.join(self.destination_folder, category)
                self._ensure_folder(category_folder)

                destination_path = os.path.join(category_folder, entry)
                destination_path = self._resolve_name_clash(destination_path)

                shutil.move(entry_path, destination_path)

                self.category_counts[category] = self.category_counts.get(category, 0) + 1
                self.moved_files.append((entry, category, destination_path))

            except (OSError, shutil.Error) as e:
                self.errors.append((entry, str(e)))

        return self.moved_files
    def _resolve_name_clash(destination_path):
        if not os.path.exists(destination_path):
            return destination_path

        folder, file_name = os.path.split(destination_path)
        name, ext = os.path.splitext(file_name)
        counter = 1
        new_path = destination_path
        while os.path.exists(new_path):
            new_path = os.path.join(folder, f"{name} ({counter}){ext}")
            counter += 1
        return new_path

    def generate_report(self, report_path="report.txt"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = []
        lines.append("FILE ORGANIZER - SUMMARY REPORT")
        lines.append("=" * 40)
        lines.append(f"Timestamp of execution : {timestamp}")
        lines.append(f"Source folder          : {self.source_folder}")
        lines.append(f"Destination folder     : {self.destination_folder}")
        lines.append(f"Total files processed  : {self.total_processed}")
        lines.append("")

        lines.append("Files per category:")
        if self.category_counts:
            for category, count in sorted(self.category_counts.items()):
                lines.append(f"  - {category}: {count}")
        else:
            lines.append("  (no files were moved)")
        lines.append("")

        lines.append("List of moved files:")
        if self.moved_files:
            for original_name, category, new_path in self.moved_files:
                lines.append(f"  - {original_name}  ->  [{category}]  {new_path}")
        else:
            lines.append("  (none)")
        lines.append("")

        if self.errors:
            lines.append("Errors encountered:")
            for file_name, error_message in self.errors:
                lines.append(f"  - {file_name}: {error_message}")
            lines.append("")

        report_text = "\n".join(lines)

        try:
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(report_text)
        except OSError as e:
            raise OSError(f"Could not write report file '{report_path}': {e}")

        return report_text