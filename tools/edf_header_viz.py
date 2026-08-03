"""
Drop-in helpers for visualizing an EDF header.
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def print_header_summary(h: dict, labels, n_samp):
    print("=" * 60)
    print("EDF HEADER SUMMARY")
    print("=" * 60)
    print(f"  Version        : {h['version']}")
    print(f"  Patient        : {h['patient']}")
    print(f"  Recording      : {h['recording']}")
    print(f"  Start date     : {h['startdate']}")
    print(f"  Start time     : {h['starttime']}")
    print(f"  Header bytes   : {h['n_header_bytes']}")
    print(f"  # Records      : {h['n_records']}")
    print(f"  Record dur (s) : {h['record_dur']}")
    print(f"  # Signals      : {h['n_signals']}")
    print("-" * 60)
    print(f"  {'#':<4}{'Label':<20}{'Samples/record':<18}{'Sample rate (Hz)':<18}")
    for i, (lbl, n) in enumerate(zip(labels, n_samp)):
        rate = n / h['record_dur'] if h['record_dur'] else 0
        print(f"  {i:<4}{lbl:<20}{n:<18}{rate:<18.2f}")
    print("=" * 60)


def plot_header_summary(h: dict, labels, n_samp,
                         out_path: str = "header_summary.png"):
    """
    Renders a fixed-size metadata table, plus TWO SEPARATE grouped charts:
      1. Real signal channels, grouped by sample rate (a genuine rate)
      2. Annotation channels, shown separately as a channel count
         (NOT a rate -- mixing these into one chart under a "sample
         rate" title was misleading, since annotation channels don't
         have a real sampling rate at all)

    Both charts group identical channels into one bar rather than one
    bar per channel, so this stays compact and readable even with
    30-100+ real channels (which would otherwise merge into one solid,
    unreadable block of color).
    """
    rates = [n / h['record_dur'] if h['record_dur'] else 0 for n in n_samp]
    is_annot = ['Annotations' in lbl for lbl in labels]

    # Split into two populations up front -- they're fundamentally
    # different kinds of channels and shouldn't share one chart/axis.
    signal_rates = [r for r, a in zip(rates, is_annot) if not a]
    annot_labels = [lbl for lbl, a in zip(labels, is_annot) if a]

    # Group signal channels by rate -> count
    signal_groups = {}
    for r in signal_rates:
        key = round(r, 3)
        signal_groups[key] = signal_groups.get(key, 0) + 1
    signal_items = sorted(signal_groups.items(), key=lambda kv: -kv[0])
    signal_bar_labels = [f"{rate:g} Hz ({n} channel{'s' if n != 1 else ''})"
                          for rate, n in signal_items]
    signal_bar_counts = [n for _, n in signal_items]

    # Annotation channels: just a count, no rate implied at all
    n_annot = len(annot_labels)

    # ── Layout: fixed-size table, then one panel per population that
    # actually has data. Heights scale only with each panel's own
    # number of distinct groups, never with total channel count.
    table_height = 2.6
    signal_panel_height = max(1.2, 0.4 * len(signal_bar_labels))
    show_annot_panel = n_annot > 0
    annot_panel_height = 1.0 if show_annot_panel else 0

    n_rows = 2 + (1 if show_annot_panel else 0)
    height_ratios = [table_height, signal_panel_height]
    if show_annot_panel:
        height_ratios.append(annot_panel_height)

    fig_height = sum(height_ratios) + 0.8  # margin for titles/spacing
    fig = plt.figure(figsize=(10, fig_height))
    gs = fig.add_gridspec(n_rows, 1, height_ratios=height_ratios, hspace=0.7)

    ax_table = fig.add_subplot(gs[0])
    ax_signals = fig.add_subplot(gs[1])
    ax_annot = fig.add_subplot(gs[2]) if show_annot_panel else None

    # --- metadata table (fixed size, doesn't scale with channel count) ---
    ax_table.axis('off')
    meta = [
        ("Version", h['version']),
        ("Patient", h['patient']),
        ("Recording", h['recording']),
        ("Start date", h['startdate']),
        ("Start time", h['starttime']),
        ("Header bytes", h['n_header_bytes']),
        ("# Records", h['n_records']),
        ("Record duration (s)", h['record_dur']),
        ("# Signals", h['n_signals']),
    ]
    tbl = ax_table.table(cellText=[[k, str(v)] for k, v in meta],
                         colWidths=[0.3, 0.6], loc='upper center', cellLoc='left',
                         bbox=[0.0, 0.0, 1.0, 0.85])
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    ax_table.set_title("EDF Header", fontsize=13, fontweight='bold', loc='left')

    # --- panel 1: real signal channels, grouped by genuine sample rate ---
    ax_signals.barh(signal_bar_labels, signal_bar_counts, color="#4C72B0")
    ax_signals.set_xlabel("Number of channels")
    ax_signals.set_title(
        f"Signal channels grouped by sample rate "
        f"({len(signal_rates)} total, {len(signal_bar_labels)} distinct rates)",
        fontsize=11, loc='left'
    )
    ax_signals.invert_yaxis()

    # --- panel 2: annotation channels, separate, no rate implied ---
    if show_annot_panel:
        ax_annot.barh([f"Annotation channels ({n_annot})"], [n_annot], color="#B0B0B0")
        ax_annot.set_xlabel("Number of channels (not a sample rate)")
        ax_annot.set_title("Annotation channels", fontsize=11, loc='left')
        ax_annot.set_yticks([])

    fig.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"✓ Saved header visualization → {out_path}")
    return out_path
