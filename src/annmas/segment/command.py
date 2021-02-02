import logging
import sys
import itertools

import click
import click_log
import tqdm

import pysam
import multiprocessing as mp

from inspect import getframeinfo, currentframe, getdoc

from ..utils.model import array_element_structure

from ..annotate.command import SegmentInfo
from ..annotate.command import SEGMENTS_TAG

from ..meta import VERSION


logger = logging.getLogger(__name__)
click_log.basic_config(logger)

mod_name = "segment"


@click.command(name=mod_name)
@click_log.simple_verbosity_option(logger)
@click.option(
    "-t",
    "--threads",
    type=int,
    default=mp.cpu_count() - 1,
    show_default=True,
    help="number of threads to use (0 for all)",
)
@click.option(
    "-o",
    "--output-bam",
    required=True,
    type=click.Path(exists=False),
    help="segment-annotated bam output",
)
@click.option(
    "-s",
    "--do-simple-splitting",
    required=False,
    is_flag=True,
    default=False,
    help="Do splitting of reads based on splitter delimiters, rather than whole array structure."
    "This splitting will cause delimiter sequences to be repeated in each read they bound.",
)
@click.option(
    "-k",
    "--keep-delimiters",
    required=False,
    is_flag=True,
    default=False,
    help="If True, will keep the delimiter sequences in the resulting reads.  Otherwise the delimiter sequence "
         "information will be removed from the resulting reads (the annotations for the delimiters will be preserved).",
)
@click.argument("input-bam", type=click.Path(exists=True))
def main(threads, output_bam, do_simple_splitting, keep_delimiters, input_bam):
    """Segment pre-annotated reads from an input BAM file."""
    logger.info(f"annmas: {mod_name} started")

    threads = mp.cpu_count() if threads <= 0 or threads > mp.cpu_count() else threads
    logger.info(f"Running with {threads} process(es)")

    if do_simple_splitting:
        logger.info("Using simple splitting mode.")
    else:
        logger.info("Using bounded region splitting mode.")

    # Configure process manager:
    # NOTE: We're using processes to overcome the Global Interpreter Lock.
    manager = mp.Manager()
    process_input_data_queue = manager.Queue(threads)
    results = manager.Queue()

    # Start worker sub-processes:
    worker_process_pool = []
    for _ in range(threads):
        p = mp.Process(target=_sub_process_work_fn, args=(process_input_data_queue, results))
        p.start()
        worker_process_pool.append(p)

    pysam.set_verbosity(0)  # silence message about the .bai file not being found
    with pysam.AlignmentFile(
        input_bam, "rb", check_sq=False, require_index=False
    ) as bam_file, tqdm.tqdm(desc="Progress", unit=" read", colour="green", file=sys.stdout) as pbar:

        # Get our header from the input bam file:
        out_bam_header_dict = bam_file.header.to_dict()

        # Add our program group to it:
        pg_dict = {
            "ID": f"annmas-{mod_name}-{VERSION}",
            "PN": "annmas",
            "VN": f"{VERSION}",
            # Use reflection to get the doc string for this main function for our header:
            "DS": getdoc(globals()[getframeinfo(currentframe()).function]),
            "CL": " ".join(sys.argv),
        }
        if "PG" in out_bam_header_dict:
            out_bam_header_dict["PG"].append(pg_dict)
        else:
            out_bam_header_dict["PG"] = [pg_dict]
        out_header = pysam.AlignmentHeader.from_dict(out_bam_header_dict)

        # Start output worker:
        output_worker = mp.Process(
            target=_sub_process_write_fn,
            args=(results, out_header, output_bam, pbar, do_simple_splitting, keep_delimiters)
        )
        output_worker.start()

        # Add in a `None` sentinel value at the end of the queue - one for each subprocess - so we guarantee
        # that all subprocesses will exit:
        iter_data = itertools.chain(bam_file, (None,) * threads)
        for r in iter_data:
            if r is not None:
                process_input_data_queue.put(r.to_string())
            else:
                process_input_data_queue.put(r)

        # Wait for our input jobs to finish:
        for p in worker_process_pool:
            p.join()

        # Now that our input processes are done, we can add our exit sentinel onto the output queue and
        # wait for that process to end:
        results.put(None)
        output_worker.join()

        logger.info(f"annmas: {mod_name} finished.")


def _sub_process_work_fn(in_queue, out_queue):
    """Function to run in each subprocess.
    Extracts and returns all segments from an input read."""
    while True:
        # Wait until we get some data.
        # Note: Because we have a sentinel value None inserted at the end of the input data for each
        #       subprocess, we don't have to add a timeout - we're guaranteed each process will always have
        #       at least one element.
        raw_data = in_queue.get()

        # Check for exit sentinel:
        if raw_data is None:
            return

        # Unpack our data here:
        read = pysam.AlignedSegment.fromstring(raw_data, pysam.AlignmentHeader.from_dict(dict()))

        # Process and place our data on the output queue:
        out_queue.put(_get_segments(read))


def _sub_process_write_fn(out_queue, out_bam_header, out_bam_file_name, pbar, do_simple_splitting, keep_delimiters):
    """Thread / process fn to write out all our data."""

    num_reads_segmented = 0
    num_segments = 0

    with pysam.AlignmentFile(
            out_bam_file_name, "wb", header=out_bam_header
    ) as out_bam_file:

        while True:
            # Wait for some output data:
            raw_data = out_queue.get()

            # Check for exit sentinel:
            if raw_data is None:
                break

            # Unpack data:
            read, segments = raw_data
            read = pysam.AlignedSegment.fromstring(read, out_bam_header)

            # Obligatory log message:
            logger.debug(
                "Segments for read %s: %s",
                read.query_name,
                segments,
            )

            # Write out our segmented reads:
            num_segments += _write_segmented_read(
                read, segments, do_simple_splitting, keep_delimiters, out_bam_file
            )

            # Increment our counters:
            num_reads_segmented += 1
            pbar.update(1)

    logger.info(
        f"annmas {mod_name}: segmented {num_reads_segmented} reads with {num_segments} total segments."
    )
    return


def _write_segmented_read(read, segments, do_simple_splitting, keep_delimiters, bam_out):
    """Split and write out the segments of each read to the given bam output file
    :param read: A pysam.AlignedSegment object containing a read that has been segmented.
    :param segments: A list of SegmentInfo objects representing the segments of the given reads.
    :param do_simple_splitting: Flag to control how reads should be split.
                                If True, will use simple delimiters.
                                If False, will require reads to appear as expected in model.array_element_structure.
    :param keep_delimiters: If True, will keep the delimiter sequences in the resulting reads.
                            Otherwise the delimiter sequence information will be removed from the resulting reads
                            (the annotations for the delimiters will be preserved).
    :param bam_out: An open pysam.AlignmentFile ready to write out data.
    :return: the number of segments written.
    """

    if do_simple_splitting:
        # Create the sections on which we want to split.
        num_required_delimiters = 2

        delimiters = list()
        delimiters.append(tuple(array_element_structure[0][-num_required_delimiters:]))
        for i, structure in enumerate(array_element_structure[1:], start=1):
            # If it's the second element then we append the delmiters to those from the first.
            # This is simple splitting, after all.
            if i == 1:
                delimiters[0] = delimiters[0] + structure[0:num_required_delimiters]
            else:
                delimiters.append(tuple(structure[0:num_required_delimiters]))

        # Now we have our delimiter list.
        # We need to go through our segments and split them up.
        # Note: we assume each delimiter can occur only once.
        delimiter_match_matrix = [0 for _ in delimiters]
        delimiter_start_segments = [None for _ in delimiters]

        # We have to store the end segments as tuples in case we want to use more than one
        # segment as a delimiter:
        delimiter_end_segment_tuples = [None for _ in delimiters]

        # We do it this way so we iterate over the segments once and the delimiters many times
        # under the assumption the segment list is much longer than the delimiters.
        for seg in segments:
            # at each position go through our delimiters and track whether we're a match:
            for i, dmi in enumerate(delimiter_match_matrix):
                try:
                    if seg.name == delimiters[i][dmi]:
                        if delimiter_match_matrix[i] == 0:
                            delimiter_start_segments[i] = seg
                        delimiter_match_matrix[i] += 1
                        if delimiter_match_matrix[i] == len(delimiters[i]):
                            # We've got a full and complete match!
                            # We store the end segment we found.
                            delimiter_end_segment_tuples[i] = (
                                delimiter_start_segments[i],
                                seg,
                            )
                    else:
                        # No match at the position we expected!
                        # We need to reset our count and start segment:
                        delimiter_match_matrix[i] = 0
                        delimiter_start_segments[i] = None
                        delimiter_end_segment_tuples[i] = None
                except IndexError:
                    # We're out of range of the delimiter.
                    # This means we've hit the end and actually have a match, so we can ignore this error.
                    pass

        # OK, we've got a handle on our delimiters, so now we just need to split the sequence.
        # We do so by looking at which end delimiters are filled in, and grabbing their complementary start delimiter:

        # Sort and filter our delimiters:
        seg_delimiters = [
            (delim_index, start, end_tuple)
            for delim_index, (start, end_tuple) in enumerate(
                zip(delimiter_start_segments, delimiter_end_segment_tuples)
            )
            if end_tuple is not None
        ]
        seg_delimiters.sort(key=lambda dtuple: dtuple[1].start)

        cur_read_base_index = 0
        prev_delim_name = "START"

        for i, (di, start_seg, end_seg_tuple) in enumerate(seg_delimiters):

            seg_start_coord = cur_read_base_index
            seg_end_coord = end_seg_tuple[1].end
            delim_name = "/".join(delimiters[di])

            start_coord = seg_start_coord
            end_coord = seg_end_coord if keep_delimiters else start_seg.start - 1

            # Write our segment here:
            _write_split_array_element(
                bam_out,
                start_coord,
                end_coord,
                seg_start_coord,
                seg_end_coord,
                read,
                segments,
                delim_name,
                prev_delim_name,
            )

            cur_read_base_index = end_seg_tuple[0].start if keep_delimiters else end_seg_tuple[1].end + 1
            prev_delim_name = delim_name

        # Now we have to write out the last segment:
        seg_start_coord = cur_read_base_index
        # Subtract 1 for 0-based inclusive coords:
        seg_end_coord = len(read.query_sequence) - 1

        start_coord = seg_start_coord
        end_coord = seg_end_coord

        delim_name = "END"

        _write_split_array_element(
            bam_out, start_coord, end_coord, seg_start_coord, seg_end_coord, read, segments, delim_name, prev_delim_name
        )

        return len(seg_delimiters)
    else:
        # Here we're doing bounded region splitting.
        # This requires each read to conform to the expected read structure as defined in the model.
        # The process is similar to what is done above for simple splitting.

        # Create our delimiter list.
        delimiters = array_element_structure

        # We need to go through our segments and split them up.
        # Note: we assume each full array element can occur only once and they do not overlap.
        delimiter_match_matrix = [0 for _ in delimiters]
        delimiter_segments = [list() for _ in delimiters]
        delimiter_found = [False for _ in delimiters]
        delimiter_score = [0 for _ in delimiters]

        # We define some scoring increments here:
        match_val = 2
        indel_val = 1

        # We do it this way so we iterate over the segments once and the delimiters many times
        # under the assumption the segment list is much longer than the delimiters.
        for seg in segments:
            # at each position go through our delimiters and track whether we're a match:
            for i, dmi in enumerate(delimiter_match_matrix):
                if not delimiter_found[i]:
                    try:
                        if seg.name == delimiters[i][dmi]:
                            delimiter_match_matrix[i] += 1
                            delimiter_segments[i].append(seg)
                            delimiter_score[i] += match_val
                        else:
                            found = False
                            # Only look ahead if we already have a partial match:
                            if delimiter_match_matrix[i] != 0:
                                # Here we "peek ahead" so we an look at other delimiters after the current one just in
                                # case we're missing a delimiter / segment.  This will impact the "score" but allow for
                                # fuzzy matching.
                                for peek_ahead in range(
                                    1, len(delimiters[i]) - delimiter_match_matrix[i]
                                ):
                                    if seg.name == delimiters[i][dmi + peek_ahead]:
                                        delimiter_match_matrix[i] += 1 + peek_ahead
                                        delimiter_segments[i].append(seg)
                                        delimiter_score[i] += indel_val
                                        found = True
                                        break

                            if not found:
                                # No match at the position we expected!
                                # We need to reset our count and start segment:
                                delimiter_match_matrix[i] = 0
                                delimiter_segments[i] = list()
                                delimiter_score[i] = 0

                        # Make sure we mark ourselves as done if we're out of info:
                        if delimiter_match_matrix[i] == len(delimiters[i]):
                            delimiter_found[i] = True

                    except IndexError:
                        # We're out of range of the delimiter.
                        # This means we've hit the end and actually have a match.
                        delimiter_found[i] = True

        # Now we have our segments as described by our model.
        # We assume they don't overlap and we write them out:
        for i, seg_list in enumerate(delimiter_segments):
            if delimiter_found[i]:

                start_seg = seg_list[0]
                end_seg = seg_list[-1]

                # If we don't want to keep delimiters we chop off the start and end segments
                # under the assumption that those are the delimiters we want to skip:
                start_coord = start_seg.start if keep_delimiters else seg_list[1].start
                end_coord = end_seg.end if keep_delimiters else seg_list[-2].end
                start_seg_coord = start_seg.start
                end_seg_coord = end_seg.end

                start_delim_name = seg_list[0].name
                end_delim_name = seg_list[-1].name

                # Write our segment here:
                _write_split_array_element(
                    bam_out,
                    start_coord,
                    end_coord,
                    start_seg_coord,
                    end_seg_coord,
                    read,
                    seg_list,
                    end_delim_name,
                    start_delim_name,
                )

        # Return the number of array elements.
        # NOTE: this works because booleans are a subset of integers in python.
        return sum(delimiter_found)


def _transform_to_rc_coords(start, end, read_length):
    """Transforms the given start and end coordinates into the RC equivalents using the given read_length."""
    return read_length - end - 1, read_length - start - 1


def _write_split_array_element(
    bam_out, start_coord, end_coord, seg_start_coord, seg_end_coord, read, segments, delim_name, prev_delim_name
):
    """Write out an individual array element that has been split out according to the given coordinates."""
    a = pysam.AlignedSegment()
    a.query_name = (
        f"{read.query_name}_{start_coord}-{end_coord}_{prev_delim_name}-{delim_name}"
    )
    # Add one to end_coord because coordinates are inclusive:
    a.query_sequence = f"{read.query_sequence[start_coord:end_coord+1]}"
    a.query_qualities = read.query_alignment_qualities[start_coord:end_coord+1]
    a.tags = read.get_tags()
    a.flag = 4  # unmapped flag
    a.mapping_quality = 255

    # Set our segments tag to only include the segments in this read:
    a.set_tag(
        SEGMENTS_TAG,
        ",".join([s.to_tag() for s in segments if seg_start_coord <= s.start <= seg_end_coord]),
    )

    bam_out.write(a)


def _get_segments(read):
    """Get the segments corresponding to a particular read by reading the segments tag information."""
    return read.to_string(), [SegmentInfo.from_tag(s) for s in read.get_tag(SEGMENTS_TAG).split("|")]
