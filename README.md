# File Organizer

A simple Python tool that automatically organizes files in a folder by
type (Images, Documents, PDFs, Spreadsheets, Scripts, etc.) and generates
a summary report of what it did.

## Project Overview

This project reads every file inside a source folder, figures out its
category based on its file extension, moves it into a matching category
subfolder, and produces a `.txt` report summarizing the whole operation
(files processed, files per category, list of moved files, and a
timestamp).

It's built using an object-oriented approach: all the logic lives inside
a `FileOrganizer` class in `organizer.py`, and `main.py` is the small
script that actually runs it.

## Features

- Reads all files from a given folder.
- Automatically creates category folders (Images, Documents, PDFs,
  Spreadsheets, Presentations, Scripts, Archives, Audio, Video, Others)
  based on file extension.
- Moves each file into its matching category folder.
- Avoids overwriting files: if a file with the same name already exists
  in the destination, it appends a number, e.g. `report (1).txt`.
- Generates a summary report containing:
  - Total number of files processed
  - Number of files in each category
  - A full list of moved files (with source name and destination path)
  - Timestamp of execution
- Saves the report as `report.txt`.
- Handles errors gracefully (missing folder, permission issues, etc.)
  using `try/except`, and lists any errors in the report.

## How to Run the Project

1. Make sure you have Python 3 installed (no extra packages needed —
   only the standard library is used).
2. Place the files you want organized inside the `sample_files/` folder
   (a set of sample files is already included).
3. From the `FileOrganizer/` folder, run:

   ```bash
   python main.py
   ```

   This organizes everything in `sample_files/` into `organized_files/`
   and writes `report.txt`.

4. You can also specify a custom source and destination folder:

   ```bash
   python main.py <source_folder> <destination_folder>
   ```

   Example:

   ```bash
   python main.py my_downloads sorted_downloads
   ```

## Folder Structure

```
FileOrganizer/
  |-- main.py             # entry point, run this to organize files
  |-- organizer.py         # FileOrganizer class (OOP logic)
  |-- report.txt            # generated summary report (after running)
  |-- README.md
  |-- sample_files/         # sample input files used for testing
  |-- organized_files/      # output: files sorted into category folders
```

## Concepts Covered

- Python fundamentals
- Object-Oriented Programming (`FileOrganizer` class)
- File handling
- `os`, `shutil`, and `datetime` modules
- Exception handling
