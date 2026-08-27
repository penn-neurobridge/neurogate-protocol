"""
main.py

De-identify an EDF/EDF+ file: extract and remove annotation event text,
anonymize header PHI fields, and produce a QA report for manual review.

Usage:
    python3 main.py <input.edf> <output_dir>

Output layout:
    <output_dir>/<patient_id>/Deidentified/<patient_id>_no_annotations.edf
    <output_dir>/<patient_id>/Extracted/<patient_id>_annotations.txt
    <output_dir>/<patient_id>/QA/<patient_id>_qa_report.txt
"""

from __future__ import annotations
import argparse
import csv
import logging
import os
import sys
import time
from dataclasses import dataclass

from edf_header import (parse_header, anonymize_main_header_bytes, find_annotation_channels,
                         EdfHeader, parse_recording_start_datetime, onset_to_datetime)
import edf_header as edf_header_mod
from edf_records import process_records
from edf_qa import build_report


@dataclass
class OutputPaths:
    deidentified_dir: str
    extracted_dir: str
    qa_dir: str


def parse_args():
    parser = argparse.ArgumentParser(
        description='De-identify an EDF/EDF+ file: extract/remove annotations, anonymize header, produce QA report.'
    )
    parser.add_argument('input_edf', help='Path to the input .edf file')
    parser.add_argument('output_dir', help='Directory to save outputs')
    return parser.parse_args()


def setup_logging(log_path: str) -> logging.Logger:
    logger = logging.getLogger('edf_deid')
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter('%(message)s')

    file_handler = logging.FileHandler(log_path, mode='w', encoding='utf-8')
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger


def build_anonymized_header(original: EdfHeader) -> EdfHeader:
    """Produces a new EdfHeader reflecting anonymized main-header bytes,
    for use both in writing the output file and in the QA field dump."""
    anon_main_bytes = anonymize_main_header_bytes(original.raw_bytes[:256])
    anon_raw = anon_main_bytes + original.raw_bytes[256:]

    anon_main = edf_header_mod.parse_main_header(anon_main_bytes)
    return EdfHeader(main=anon_main, signals=original.signals, raw_bytes=anon_raw)


def process(input_path: str, patient_id: str, paths: OutputPaths) -> bool:
    overall_start = time.perf_counter()

    output_edf_path = os.path.join(paths.deidentified_dir, f'{patient_id}_no_annotations.edf')
    annotations_csv_path = os.path.join(paths.extracted_dir, f'{patient_id}_annotations.csv')
    qa_report_path = os.path.join(paths.qa_dir, f'{patient_id}_qa_report.txt')
    log_path = os.path.join(paths.qa_dir, f'{patient_id}_run_log.txt')

    logger = setup_logging(log_path)

    if not os.path.exists(input_path):
        logger.error(f"ABORTED — file does not exist: {input_path}")
        return False

    file_size = os.path.getsize(input_path)
    logger.info(f"Input: {input_path} ({file_size:,} bytes)")

    with open(input_path, 'rb') as f:
        original_header = parse_header(f)

    logger.info(f"Signals: {original_header.main.n_signals}, "
                f"Records: {original_header.main.n_records}, "
                f"Header bytes: {original_header.main.n_header_bytes}")

    annot_indices = find_annotation_channels(original_header.signals)
    if not annot_indices:
        logger.info("No 'EDF Annotations' channel found — plain EDF, nothing to strip. "
                     "Header PHI fields were not anonymized.")
        return False

    logger.info(f"Annotation channel(s): {annot_indices}")

    anonymized_header = build_anonymized_header(original_header)

    logger.info("Running combined pass: extract annotations, strip event text, "
                "hash-check integrity, scan for leaked text...")

    def progress(done, total, elapsed):
        pct = done / total * 100
        print(f"  {done:>8}/{total} ({pct:5.1f}%)  elapsed {elapsed:6.1f}s", end='\r', flush=True)

    pass_result = process_records(
        input_path=input_path,
        output_path=output_edf_path,
        header=anonymized_header,   # writes the anonymized header bytes to the output file
        annot_indices=annot_indices,
        progress_cb=progress,
    )
    print()  # move off the \r line

    logger.info(f"Pass complete in {pass_result.elapsed_sec:.1f}s — "
                f"{len(pass_result.annotations)} annotations extracted, "
                f"{len(pass_result.printable_findings)} printable-ASCII finding(s)")
    for line in pass_result.timings.as_report_lines(pass_result.elapsed_sec):
        logger.info(line)

    start_dt = parse_recording_start_datetime(original_header.main)
    if start_dt is None:
        logger.info("Recording start date/time could not be parsed -- "
                     "'clock_datetime' column will be blank.")

    with open(annotations_csv_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['channel_name', 'channel_number', 'onset_sec',
                          'clock_datetime', 'duration_sec', 'description'])
        for chan_idx, chan_label, onset, dur, desc in pass_result.annotations:
            clock_str = onset_to_datetime(start_dt, onset).strftime('%Y-%m-%d %H:%M:%S') if start_dt else ''
            writer.writerow([chan_label, chan_idx, f'{onset:.6f}', clock_str, f'{dur:.6f}', desc])
    logger.info(f"Annotations saved -> {annotations_csv_path}")

    report_text = build_report(
        patient_id=patient_id,
        input_path=input_path,
        original_header=original_header,
        anonymized_header=anonymized_header,
        annot_indices=annot_indices,
        pass_result=pass_result,
        annotations_csv_path=annotations_csv_path,
        output_edf_path=output_edf_path,
        total_runtime_sec=time.perf_counter() - overall_start,
    )
    with open(qa_report_path, 'w', encoding='utf-8') as f:
        f.write(report_text)

    logger.info(f"QA report -> {qa_report_path}")
    logger.info(f"Total time: {time.perf_counter() - overall_start:.1f}s")

    return 'OVERALL: PASS' in report_text


if __name__ == '__main__':
    args = parse_args()

    patient_id = os.path.splitext(os.path.basename(args.input_edf))[0]
    patient_dir = os.path.join(args.output_dir, patient_id)

    paths = OutputPaths(
        deidentified_dir=os.path.join(patient_dir, 'Deidentified'),
        extracted_dir=os.path.join(patient_dir, 'Extracted'),
        qa_dir=os.path.join(patient_dir, 'QA'),
    )
    for d in (paths.deidentified_dir, paths.extracted_dir, paths.qa_dir):
        os.makedirs(d, exist_ok=True)

    ok = process(args.input_edf, patient_id, paths)
    sys.exit(0 if ok else 1)
