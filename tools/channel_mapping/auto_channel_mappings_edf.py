import os
import re
import sys
import argparse
from collections import Counter
from datetime import datetime

import numpy as np
from edf_reader import EdfWrapper


# Intracranial C-contact channels: C followed by 2-3 digits (e.g. C12, C129).
# Single-digit scalp names like C3/C4 are excluded.
CXXX_PATTERN = re.compile(r"^C\d{2,3}$")

# Trailing DC channels appended after the first Cxx/Cxxx block, keyed by channel index.
DC_RULES_BY_INDEX = {
    "low": [(128, "DC01"), (134, "DC07")],   # first Cxx/Cxxx at index <= 128
    "high": [(256, "DC01"), (262, "DC07")],  # first Cxx/Cxxx at index > 128
}


def get_edf_files_in_directory(path):
    # Recursively list all .edf files (case-insensitive) under path
    root = os.path.abspath(path)
    edf_files = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if name.lower().endswith(".edf"):
                edf_files.append(os.path.join(dirpath, name))
    return sorted(edf_files)

def _ch_name(info):
    # edf-reader basic-info dicts use the pymef-style 'name' key
    return info.get('name', info.get('label'))


def read_edf_file_info(file_path):
    # Read channel labels and recording start time via edf-reader (supports EDF+D).
    reader = EdfWrapper(file_path)
    try:
        channels = [_ch_name(ch) for ch in reader.read_ts_channel_basic_info()]
        start_dt = reader.header.get("start_datetime")
        return channels, start_dt
    finally:
        reader.close()

def format_start_datetime(dt):
    if dt is None:
        return ""
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return str(dt)

def save_file_index(records, filename):
    # records: list of dicts with edf_file, start_datetime, mapping_file, mapping_final_file
    sorted_records = sorted(
        records,
        key=lambda rec: rec["start_datetime"] if rec["start_datetime"] is not None else datetime.max,
    )
    with open(filename, "w", encoding="utf-8") as file:
        file.write("filename\tstart_datetime\tchannel_mapping\tchannel_mapping_final\n")
        for rec in sorted_records:
            file.write(
                f"{rec['edf_file']}\t"
                f"{format_start_datetime(rec['start_datetime'])}\t"
                f"{rec['mapping_file']}\t"
                f"{rec['mapping_final_file']}\n"
            )

def write_unique_mappings(path, ch_lists):
    # Deduplicate channel orders and write one mapping pair per unique layout.
    order_to_id = {}
    unique_orders = {}
    next_id = 1
    for ch_list in ch_lists:
        key = tuple(ch_list)
        if key not in order_to_id:
            order_to_id[key] = next_id
            unique_orders[key] = next_id
            next_id += 1

    for key, mapping_id in unique_orders.items():
        ch_list = list(key)
        mapping_file = f"channel_mapping_{mapping_id}.txt"
        mapping_final_file = f"channel_mapping_final_{mapping_id}.txt"
        save_list_to_file(ch_list, os.path.join(path, mapping_file))
        save_final_mapping(ch_list, os.path.join(path, mapping_final_file))
        print(f"Wrote unique mapping {mapping_id}: {mapping_file}, {mapping_final_file}")

    return order_to_id

def are_all_lists_equal(list_of_lists):
    # Check if all lists are identical to the first list
    return all(lst == list_of_lists[0] for lst in list_of_lists)

# Scalp EEG lead prefixes (from utils.check_channel_type)
SCALP_LIST = ["O","C","CZ","F","FP","FZ","T","P","PZ","FPZ","A","M","EKG","ECG","EMG",'LOC','ROC']


def clean_labels(channel_li):
    """Clean and standardize channel labels (from utils.py)."""
    if isinstance(channel_li, str):
        channel_li = [channel_li]
    new_channels = []
    for i in range(len(channel_li)):
        label_num_search = re.search(r"\d", channel_li[i])
        if label_num_search is not None:
            label_num_idx = label_num_search.start()
            label_non_num = channel_li[i][:label_num_idx]
            label_num = channel_li[i][label_num_idx:]
            label_num = label_num.lstrip("0")
            label = label_non_num + label_num
        else:
            label = channel_li[i]
        label = label.replace("EEG", "")
        label = label.replace("Ref", "")
        label = label.replace(" ", "")
        label = label.replace("-", "")
        label = label.replace("CAR", "")
        label = label.replace("HIPP", "DH")
        label = label.replace("AMY", "DA")
        label = label.replace("FP", "Fp")
        label = label.replace("CZ", "Cz")
        label = label.replace("FZ", "Fz")
        label = label.replace("PZ", "Pz")
        label = label.replace("PZ", "Pz")
        label = label.replace("FPz", "Fpz")
        label = label.replace("FPZ", "Fpz")
        label = "T3" if label == "T7" else label
        label = "T4" if label == "T8" else label
        label = "T5" if label == "P7" else label
        label = "T6" if label == "P8" else label
        new_channels.append(label)
    return np.array(new_channels)


def format_channel(item):

    label_num_search = re.search(r"\d", item)
    if label_num_search is not None:
        label_non_num = item[:label_num_search.start()]
        label_num = item[label_num_search.start():]
    else:
        label_non_num = item
        label_num = ""

    lead = label_non_num.upper().replace(" ", "")
    if lead in SCALP_LIST:
        return str(clean_labels(item)[0])

    # Non-scalp: pad single-digit endings with a leading 0
    if label_num and len(label_num) == 1 and label_num.isdigit():
        return f"{label_non_num}0{label_num}"
    return item

def save_list_to_file(list_data, filename):
    with open(filename, 'w') as file:
        for index, item in enumerate(list_data):
            # Write the index and the formatted item to the file
            file.write(f"{index} {format_channel(item)}\n")

def save_final_mapping(list_data, filename):
    # Drop the first Cxx/Cxxx channel and everything after it, then append trailing
    # DC channels based on that channel's index in the original list.
    first_cxxx_idx = None
    for i, item in enumerate(list_data):
        if CXXX_PATTERN.match(item):
            first_cxxx_idx = i
            break

    kept = list_data[:first_cxxx_idx] if first_cxxx_idx is not None else list(list_data)

    with open(filename, 'w') as file:
        for index, item in enumerate(kept):
            file.write(f"{index} {format_channel(item)}\n")
        if first_cxxx_idx is not None:
            rule_key = "low" if first_cxxx_idx <= 128 else "high"
            for idx, label in DC_RULES_BY_INDEX[rule_key]:
                file.write(f"{idx} {label}\n")


def get_most_frequent_order(ch_lists):
    # Pick the channel order that appears most often across readable files
    candidates = [tuple(c) for c in ch_lists if c and c != ['']]
    if not candidates:
        return []
    most_common_order, _ = Counter(candidates).most_common(1)[0]
    return list(most_common_order)


def main():
    parser = argparse.ArgumentParser(
        description="Generate channel mappings from EDF files, optionally enforcing a uniform channel order."
    )
    parser.add_argument("-p","--path", help="Directory containing .edf files")
    args = parser.parse_args()

    path = args.path
    if not os.path.isdir(path):
        print(f"Error: directory does not exist: {path}")
        sys.exit(1)

    datasets = get_edf_files_in_directory(path)
    if not datasets:
        print(f"Error: no .edf files found in directory: {path}")
        sys.exit(1)

    ch_lists = []
    start_times = []
    for edf_file in datasets:
        try:
            channels, start_dt = read_edf_file_info(edf_file)
            ch_lists.append(channels)
            start_times.append(start_dt)
        except Exception as e:
            print(f"An error occurred: {e}")  # Print the original error message
            print(edf_file)
            ch_lists.append([''])
            start_times.append(None)

    rel_paths = [os.path.relpath(f, path) for f in datasets]

    if are_all_lists_equal(ch_lists):
        data = ch_lists[0]
        mapping_file = "channel_mapping.txt"
        mapping_final_file = "channel_mapping_final.txt"
        save_list_to_file(data, os.path.join(path, mapping_file))
        save_final_mapping(data, os.path.join(path, mapping_final_file))
        index_records = [
            {
                "edf_file": rel_path,
                "start_datetime": start_dt,
                "mapping_file": mapping_file,
                "mapping_final_file": mapping_final_file,
            }
            for rel_path, start_dt in zip(rel_paths, start_times)
        ]
        save_file_index(index_records, os.path.join(path, "edf_channel_mapping_index.txt"))
        print(f"Wrote {mapping_file}, {mapping_final_file}, edf_channel_mapping_index.txt")
        return

    print("Warning: channel mappings across datasets don't match!")

    order_to_id = write_unique_mappings(path, ch_lists)
    index_records = []
    for rel_path, ch_list, start_dt in zip(rel_paths, ch_lists, start_times):
        mapping_id = order_to_id[tuple(ch_list)]
        index_records.append({
            "edf_file": rel_path,
            "start_datetime": start_dt,
            "mapping_file": f"channel_mapping_{mapping_id}.txt",
            "mapping_final_file": f"channel_mapping_final_{mapping_id}.txt",
        })

    save_file_index(index_records, os.path.join(path, "edf_channel_mapping_index.txt"))
    print(f"Wrote {len(set(order_to_id.values()))} unique mapping(s) and edf_channel_mapping_index.txt")

if __name__ == "__main__":
    main()
