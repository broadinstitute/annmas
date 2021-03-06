import logging
import time
import sys
import os
import re
import math
import ssw

import click
import click_log

import gzip
import pysam
from collections import OrderedDict
from construct import *

import matplotlib.pyplot as plt
from matplotlib import transforms

from ..utils.model import LibraryModel
from ..utils.model import reverse_complement
from ..utils import bam_utils

logging.basicConfig(stream=sys.stderr)
logger = logging.getLogger("inspect")
click_log.basic_config(logger)


@click.command(name=logger.name)
@click_log.simple_verbosity_option(logger)
@click.option(
    "-r",
    "--read-names",
    type=str,
    multiple=True,
    help="read names (or file(s) of read names) to inspect",
)
@click.option(
    "-p",
    "--pbi",
    required=False,
    type=click.Path(exists=True),
    help="BAM .pbi index file",
)
@click.option(
    "-f",
    "--file-format",
    default="png",
    type=click.Choice(["png", "pdf"]),
    help="Image file format",
)
@click.option(
    "-o",
    "--outdir",
    default=".",
    required=False,
    type=click.Path(exists=False),
    help="Output directory",
)
@click.option(
    '--m10',
    is_flag=True,
    default=False,
    show_default=True,
    help="Use the 10 array element MAS-seq model."
)
@click.option(
    '--seg-score',
    is_flag=True,
    default=False,
    show_default=True,
    help="Display alignment score for annotated segments."
)
@click.argument("input-bam", type=click.Path(exists=True))
def main(read_names, pbi, file_format, outdir, m10, seg_score, input_bam):
    """Inspect the classification results on specified reads."""

    t_start = time.time()

    logger.info("Invoked via: longbow %s", " ".join(sys.argv[1:]))

    pbi = f"{input_bam}.pbi" if pbi is None else pbi
    if not os.path.exists(pbi):
        raise FileNotFoundError(f"Missing .pbi file for {input_bam}")

    if not os.path.exists(outdir):
        os.makedirs(outdir)

    if m10:
        logger.info("Using MAS-seq 10 array element annotation model.")
        lb_model = LibraryModel.build_and_return_mas_seq_10_model()
    else:
        logger.info("Using MAS-seq default annotation model.")
        lb_model = LibraryModel.build_and_return_mas_seq_model()

    # Create an aligner if we are scoring our segments:
    ssw_aligner = ssw.Aligner() if seg_score else None

    # Open our bam file:
    pysam.set_verbosity(0)
    with pysam.AlignmentFile(input_bam, "rb", check_sq=False, require_index=False) as bf:
        # If we have read names, we should use them to inspect the file:
        if len(read_names) > 0:
            file_offsets = load_read_offsets(pbi, load_read_names(read_names))

            for i, z in enumerate(file_offsets):

                if not file_offsets[z]["offset"]:
                    logger.error("Read not in index file: %s", read_names[i])
                    sys.exit(1)

                bf.seek(file_offsets[z]["offset"])
                read = bf.__next__()
                __create_read_figure(file_format, lb_model, outdir, read, seg_score, ssw_aligner)
        else:
            # Without read names we just inspect every read in the file:
            logger.info("No read names given.  Inspecting every read in the input bam file.")
            for read in bf:
                __create_read_figure(file_format, lb_model, outdir, read, seg_score, ssw_aligner)

    logger.info(f"Done. Elapsed time: %2.2fs.", time.time() - t_start)


def __create_read_figure(file_format, lb_model, outdir, read, seg_score, ssw_aligner):
    """Create a figure for the given read."""

    out = f'{outdir}/{re.sub("/", "_", read.query_name)}.{file_format}'

    seq, path, logp = annotate_read(read, lb_model)

    logger.info("Drawing read '%s' to '%s'", read.query_name, out)
    draw_state_sequence(seq, path, logp, read, out, seg_score, lb_model, ssw_aligner, size=13, family="monospace")


def load_read_names(read_name_args):
    read_names = []

    for r in read_name_args:
        if os.path.exists(r):
            with open(r, "r") as f:
                for i, line in enumerate(f):
                    # Space / Tab / Newline / Line feed are all forbidden in read names by the sam spec, so we can
                    # trim it all off:
                    rn = line.strip()
                    if not bam_utils.PB_READ_NAME_RE.match(rn):
                        logger.error(
                            "Read name on line %d of file %s doesn't appear to be a PacBio read: %s", i, r, rn
                        )
                        sys.exit(1)

                    read_names.append(rn)
        else:
            if not bam_utils.PB_READ_NAME_RE.match(r):
                logger.error("Read name doesn't appear to be a PacBio read: %s", r)
                sys.exit(1)
            read_names.append(r)

    return read_names


def load_read_offsets(pbi_file, read_names):
    """
    Compute file offsets for specified read names
    """

    # Decode PacBio .pbi file.  This is not a full decode of the index, only the parts we need for read selection.
    # More on index format at https://pacbiofileformats.readthedocs.io/en/9.0/PacBioBamIndex.html .

    fmt = Struct(
        # Header
        "magic" / Const(b"PBI\x01"),
        "version_patch" / Int8ul,
        "version_minor" / Int8ul,
        "version_major" / Int8ul,
        "version_empty" / Int8ul,
        "pbi_flags" / Int16ul,
        "n_reads" / Int32ul,
        "reserved" / Padding(18),
        # Basic information section (columnar format)
        "rgId" / Padding(this.n_reads * 4),
        "qStart" / Padding(this.n_reads * 4),
        "qEnd" / Array(this.n_reads, Int32sl),
        "holeNumber" / Array(this.n_reads, Int32sl),
        "readQual" / Padding(this.n_reads * 4),
        "ctxtFlag" / Padding(this.n_reads * 1),
        "fileOffset" / Array(this.n_reads, Int64sl),
    )

    # Make a list of bgzf virtual file offsets for sharding and store ZMW counts.
    file_offsets_hash = OrderedDict()
    for read_name in read_names:
        name_pieces = re.split("/", read_name)

        hole_number = int(name_pieces[1])
        if "ccs" in name_pieces:
            if len(name_pieces) > 3:
                rr = re.split("_", name_pieces[3])
                range_end = int(rr[1]) - int(rr[0]) + 1
            else:
                range_end = -1
        else:
            range_end = re.split("_", name_pieces[2])[1]

        file_offsets_hash[f"{hole_number}_{range_end}"] = {"read_name": read_name, "offset": None}

    with gzip.open(pbi_file, "rb") as f:
        idx_contents = fmt.parse_stream(f)

        for j in range(0, idx_contents.n_reads):
            key1 = f"{idx_contents.holeNumber[j]}_{idx_contents.qEnd[j]}"
            key2 = f"{idx_contents.holeNumber[j]}_-1"

            # Save virtual file offsets for the selected reads only
            if key1 in file_offsets_hash:
                file_offsets_hash[key1]["offset"] = idx_contents.fileOffset[j]
            elif key2 in file_offsets_hash:
                file_offsets_hash[key2]["offset"] = idx_contents.fileOffset[j]

    return file_offsets_hash


def annotate_read(read, m):
    flogp = -math.inf
    fseq = read.query_sequence
    fppath = []

    if read.has_tag("SG"):
        tag = re.split(",", read.get_tag("SG"))

        for e in tag:
            state, rrange = re.split(":", e)
            qStart, qEnd = re.split("-", rrange)
            qLen = int(qEnd) - int(qStart) + 1

            fppath.extend([state] * qLen)
    else:
        for seq in [read.query_sequence, reverse_complement(read.query_sequence)]:
            logp, ppath = m.annotate(seq)

            if logp > flogp:
                fseq = seq
                flogp = logp
                fppath = ppath

    return fseq, fppath, flogp


def format_state_sequence(seq, path, line_length=150):
    # TODO: Must tie this into the model itself.  We shouldn't re-define our segments here.
    color_hash = {
        "10x_Adapter": "#334D5C",
        "5p_TSO": "#334D5C",
        "Poly_A": "#45B29D",
        "3p_Adapter": "#EFC94C",
        "A": "#DF5A49",
        "B": "#DF5A49",
        "C": "#DF5A49",
        "D": "#DF5A49",
        "E": "#DF5A49",
        "F": "#DF5A49",
        "G": "#DF5A49",
        "H": "#DF5A49",
        "I": "#DF5A49",
        "J": "#DF5A49",
        "K": "#DF5A49",
        "L": "#DF5A49",
        "M": "#DF5A49",
        "N": "#DF5A49",
        "O": "#DF5A49",
        "P": "#DF5A49",
        "Q": "#DF5A49",
        "R": "#DF5A49",
        "random": "#aaaaaa",
    }

    labelled_bases = []
    state_labels = []
    state_colors = []

    for i in range(0, len(path), line_length):
        bases = []
        label = path[i]

        for j in range(i, min(i + line_length, len(path))):
            a = seq[j]
            b = path[j]

            # Handle the line breaks:
            if label == b:
                bases.append(a)
            elif label != b:
                labelled_bases.append("".join(bases))
                state_labels.append(label)
                state_colors.append(color_hash[label])

                bases = [a]
                label = b

        labelled_bases.append("".join(bases))
        state_labels.append(label)
        state_colors.append(color_hash[label])

    return labelled_bases, state_colors, state_labels


def draw_state_sequence(seq, path, logp, read, out, show_seg_score, model, ssw_aligner, **kwargs):

    line_length = 150

    base_strings, colors, labels = format_state_sequence(seq, path, line_length=line_length)

    f = plt.figure(figsize=(24, 24))

    ax = plt.gca()
    t = ax.transData
    canvas = ax.figure.canvas

    qual_string = f"[read qual: {read.get_tag('rq'):0.03f}/1.000]    " if read.has_tag("rq") else ""
    np_string = f"[# Passes: {read.get_tag('np')}]    " if read.has_tag("np") else ""

    f.suptitle(
        f"{read.query_name}\n{qual_string}{np_string}[{len(read.query_sequence)} bp]    [model score: {logp:.2f}]",
        fontsize=16
    )

    f.patch.set_visible(False)
    ax.axis("off")

    columns = line_length
    rows = 4 * 80

    plt.xlim([0, columns])
    plt.ylim([0, rows])

    letters_seen = 0
    row = 0
    column = 0
    n = 0

    for base_string, color, lbl in zip(base_strings, colors, labels):
        if column == 0:
            ytic = ax.text(0, rows - row, f"{n}  ", transform=t, ha="right")

        text = ax.text(
            0,
            rows - row,
            base_string,
            color=color,
            transform=t,
            bbox=dict(facecolor=f"{color}22", edgecolor=f"{color}22", pad=0),
            **kwargs,
        )

        # Write classified sequence
        text.draw(canvas.get_renderer())
        ex = text.get_window_extent()

        # I truly have no idea why I need the 0.72 scaling factor, but if I don't have this, PDF images
        # are super broken.
        scaling_factor = 1.00 if out.endswith(".png") else 0.72
        t = transforms.offset_copy(
            text.get_transform(), x=scaling_factor * ex.width, units="dots"
        )

        # Write state label
        if lbl != "random":

            # If we want to show the segment scores, we calculate them here:
            if show_seg_score:
                known_segment_seq = model.adapter_dict[lbl]
                alignment = ssw_aligner.align(base_string.upper(), known_segment_seq)
                optimal_score = alignment.score
                # The max score is the match score * the length of the reference segment
                max_score = len(known_segment_seq) * ssw_aligner.matrix.get_match()
                seg_score_string = f" ({optimal_score}/{max_score})"
            else:
                seg_score_string = ""

            ax.text(
                0 - (len(base_string) / 2),
                rows - row - 3.5,
                f"{lbl}{seg_score_string}",
                transform=t,
                va="bottom",
                ha="center",
                fontsize=8,
                bbox=dict(facecolor="white", edgecolor="black"),
            )

        if lbl == "10x_Adapter":
            for o in range(10, 50, 10):
                if letters_seen + len(base_string) + o < line_length:
                    ax.text(0, rows - row + 1.7, f"{' ' * o}'", transform=t, **kwargs)

        # Decide whether we need to break into a new row
        letters_seen += len(base_string)
        n += len(base_string)
        column += 1

        if letters_seen >= columns:
            letters_seen = 0

            row += 1
            column = 0

            text = ax.text(column, row, "")
            text.draw(canvas.get_renderer())
            t = transforms.offset_copy(
                text.get_transform(), x=0, y=-30 * row, units="dots"
            )

    y2tic = ax.text(0, rows - row, f"  {n}", transform=t, ha="left")

    plt.savefig(out, bbox_inches="tight")

    plt.close()
