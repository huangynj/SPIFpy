"""A module to handle reading SEA Model 200 (M200) files.

This module will read data from an M200 file

Before reading through this code, it is highly recommended that you become
familiar with a few other topics. First, read Chapter 3 of the SEA Model 200
User's Manual (http://www.scieng.com/pdf/m200.pdf); it describes the data
format used by the M200, and by extension, the format of the data that this
program reads. Second, become familiar with Python's 'struct' module
(http://docs.python.org/2/library/struct.html); it effectively allows a
program to read in blocks of data from a file as though they were C-style
structs. Finally, Python's list comprehensions
(http://docs.python.org/2/tutorial/datastructures.html#list-comprehensions)
are used frequently when reading a file.

"""
# TODO update the above docstring

import collections
import csv
import datetime
import math
import re
import struct
import warnings

from tqdm import tqdm

from . import tag

M200_ACQUISITION_TABLE = 'ACQTBL.TXT'
M200_SECONDARY_ACQUISITION_TABLE = 'ACQ2TBL.TXT'
M300_ACQUISITION_TABLE = 'acq.300'
M300_SECONDARY_ACQUISITION_TABLE = 'saq.300'

# These are the tag numbers that are defined in the user manuals of the
# SEA Model 200 and SEA Model 300.
TIME_TAG = 0
NEXT_TAG = 999
RESERVED_TAG_LOW_END = 65000
RESERVED_TAG_HIGH_END = 65529
FILENAME_TAG = 65530
FILEDATA_TAG = 65531
COMMAND_TAG = 65532
ERROR_TAG = 65533
SAME_TAG = 65534
LAST_TAG = 65535

# These acquisition types are defined in the user manuals of the
# SEA Model 200 and SEA Model 300.
SEA_ACQUISITION_TYPE_2D_MONO_IMAGE = 5
SEA_ACQUISITION_TYPE_SEA_ANALOG_TO_DIGITAL_INPUT = 35
SEA_ACQUISITION_TYPE_2D_GREY_ADVANCED = 66
SEA_ACQUISITION_TYPE_CIP_IMAGE = 78
SEA_ACQUISITION_TYPE_NETWORK_BINARY_DATA = 85

# This struct represents the format used to store time data in the data files.
# Using this struct, the time data can be easily read from the data files.
SEA_TIME_STRUCT = struct.Struct(''.join(('<', 'H' * 9)))

# This named tuple will be used to store the components of the time data from an
# SEA file in a convenient, simple data structure.
SEATime = collections.namedtuple('SEATime',
    'year, month, day, hour, minute, second, fraction_of_second, max_sys_freq, '
    'buffer_life_span')


class SEAReadWarning(UserWarning):
    """A warning issued when unexpected data is encountered. """
    pass


class InvalidSEAFileException(Exception):
    """Raised when an invalid SEA file is passed to M200File."""
    def __init__(self, filename):
        self.filename = filename


class IncompleteDirectoryEntryException(Exception):
    """Raised when an incomplete directory entry is encountered when readig data
    from an SEA file.

    """
    pass


class UnexpectedEndOfFileException(Exception):
    """Raised when the end of a file is reached in the middle of reading a
    record.

    """
    pass


class UnexpectedEndOfBufferException(Exception):
    """Raised when the end of a buffer is reached in the middle of reading a
    record.

    """
    pass


def datetime_from_raw_seatime(sea_file_data, offset=0):
    """Parses time data from an SEA dataset return a datetime object containing
    the data.

    Arguments:
    sea_file_data -- A buffer that contains SEA data
    offset -- The offset into sea_file_data where the time data is found

    """
    seatime = SEATime(*SEA_TIME_STRUCT.unpack_from(sea_file_data, offset))

    return datetime_from_seatime(seatime)


def datetime_from_seatime(seatime):
    # - max_sys_freq is the number of clock ticks per second
    # - fraction_of_second is the number of clock ticks that have occurred in
    #   the current second
    # - multiply by 1 000 000 to convert fraction of second into microseconds
    #   (needed by datetime)
    microsecond = int(1000000 * seatime.fraction_of_second / seatime.max_sys_freq)

    try:
        datetime_obj = datetime.datetime(seatime.year, seatime.month, seatime.day,
             seatime.hour, seatime.minute, seatime.second, microsecond,
             tzinfo=None)
    except ValueError as ve:
        print(seatime)
        raise ve

    return datetime_obj


csv.register_dialect('sea_acquisition_table', delimiter=' ', quotechar='"',
                        skipinitialspace=True)


class SEAFile(object):
    """A class dedicated to reading data from a file generated by the M200.

    Public Methods:
        get_datetime -- returns a datetime object that contains the start time
            of the file
        get_tag_by_number -- returns the tag from this file that has the
            specified tag number
        get_tags_by_typ -- returns a list of all tags from this file that have
            the specified type
        iter_buffers -- return a generator that will return each buffer of this
            file in sequence

    Public Attributes:
        n/a

    """
    #TODO fix docstring above

    _ACQ_NAME = None
    _ACQ_CSV_DIALECT = 'sea_acquisition_table'
    _ACQ2_NAME = None
    _TAG_CLASS = tag.SEATag
    _TIME_DATASET_NUMBER_OF_SAMPLES = None
    _TIME_DATASET_BYTES_PER_SAMPLE = 18
    _TIME_DATASET_ADDRESS = 0xAA55

    def __init__(self, filename):
        self.filename = filename

        self._already_initialized = False
        self._buffer_types = set()
        self._config_files = []
        self._datetime = None
        self._file_contents = None
        self._secondary_tags = set()
        self._tags_by_tag_number = {}
        self._tags_by_typ = {}

        self._filetype_check()

        self._init_from_file()

    def _filetype_check(self):
        """Checks that the underlying file is a valid SEA file.

        This method does not actually verify the entire file. Instead, it
        attempts to read the first directory entry of the first directory in the
        file. This directory entry will always be for a time dataset, so we can
        compare known values for time directories to see if they match.

        """
        try:
            dir_entry = DirectoryEntry.new_from_raw_data(self._get_contents())
        except IncompleteDirectoryEntryException:
            raise InvalidSEAFileException(self.filename)

        if dir_entry.tag_number != TIME_TAG or \
                dir_entry.bytes_per_sample != \
                    self._TIME_DATASET_BYTES_PER_SAMPLE or \
                dir_entry.address != self._TIME_DATASET_ADDRESS:
            raise InvalidSEAFileException(self.filename)

    def _add_tag(self, tag):
        if tag.tag_number in self._tags_by_tag_number.keys():
            warnings.warn(
                '{:s}.add_tag: ignoring duplicate tag '
                '({:s})'.format(self.__class__.__name__, tag))
            return

        self._tags_by_tag_number[tag.tag_number] = tag
        try:
            self._tags_by_typ[tag.typ].append(tag)
        except KeyError as ke:
            if ke.args[0] == tag.typ:
                self._tags_by_typ[tag.typ] = [tag]
            else:
                raise ke

    def get_datetime(self):
        return self._datetime

    def get_tag_by_tag_number(self, tag_number):
        """Fetches a tag from self.

        Arguments:
            tag_number -- The number of the tag to retrieve

        Returns:
            An instance of Tag, owned by self, which has the given tag number

        Raises:
            KeyError -- Raised when self has no tag with the given number

        """
        return self._tags_by_tag_number[tag_number]

    def get_tags_by_typ(self, typ):
        """
        Fetches a group of tags with a given typ.

        Note: 'typ' is not a typo. It is used intentionally to avoid name
        conflicts with the type keyword/class.

        Arguments:
            typ -- The typ value of tags to be fetched.

        Returns:
            A list of all Tag instances, owned by self, which have the given typ
            attribute.

        Raises:
            KeyError -- Raised if self has no Tags of with the given typ(e).

        """

        return self._tags_by_typ[typ]

    def _get_contents(self):
        """Fetches the contents of the SEA file that self represents.

        Returns:
            A long string which holds the contents of the represented SEA file.

        Implementation Details:
            The first time this method is called, it will read in the data,
            store it in an attribute, and then return the data. On subsequent
            calls, this method simply returns the attribute.

        """

        if self._file_contents is None:
            with open(self.filename, mode='rb') as data_file:
                self._file_contents = data_file.read()

        return self._file_contents

    def _init_from_file(self):
        """Initializes self using the contents of the SEA file that self
        represents.

        This method skims through the SEA directories in the SEA file and
        reads those that contain metadata that will be needed to read the rest
        of the file. The most important aspect is reading the FILEDATA data
        containing the acquisition table, which contains descriptions and tag
        numbers for each data type contained in the file. In addition, the
        acquisition table specifies how the various data types are grouped into
        buffers.

        Arguments:
            n/a

        Returns:
            n/a

        """
        if self._already_initialized:
            return

        for dir_entries, raw_buffer in \
                self._raw_buffers({'tag_number': [FILEDATA_TAG]}):
            has_filename = False
            for dir_entry in dir_entries:
                # print(dir_entry.tag_number)
                if dir_entry.tag_number == TIME_TAG:
                    # Set a date-time value for the file as a whole, using the
                    # first time data found in the file.
                    if self._datetime is None:
                        self._datetime = datetime_from_raw_seatime(
                            raw_buffer, dir_entry.data_offset)

                elif dir_entry.tag_number == NEXT_TAG:
                    break

                elif RESERVED_TAG_LOW_END <= dir_entry.tag_number <= \
                        RESERVED_TAG_HIGH_END:
                    # reserved for future use
                    pass

                elif dir_entry.tag_number == FILENAME_TAG:
                    filename = self._unpack_string_data(
                            raw_buffer, dir_entry
                            )[0].strip(b'\0').decode('utf-8')

                    new_file = ConfigFile(filename)
                    has_filename = True

                elif dir_entry.tag_number == FILEDATA_TAG:
                    if has_filename:
                        new_file.data = self._unpack_string_data(
                            raw_buffer, dir_entry
                            )[0].strip(b'\n')

                        self._config_files.append(new_file)
                        has_filename = False

                        if new_file.name == M200_ACQUISITION_TABLE:
                            # If the tag class is still the "generic" SEATag,
                            # change it to the more specific version
                            if self._TAG_CLASS == tag.SEATag:
                                self._TAG_CLASS = tag.M200Tag
                                self._TIME_DATASET_NUMBER_OF_SAMPLES = 1
                                self._add_tag(self._TAG_CLASS('Time', -1,
                                    TIME_TAG,
                                    self._TIME_DATASET_NUMBER_OF_SAMPLES,
                                    self._TIME_DATASET_BYTES_PER_SAMPLE, 0, 0,
                                    0, 0, 0xAA55))
                                self._add_tag(self._TAG_CLASS('Filename', -1,
                                    FILENAME_TAG, 1, 0, 255, 0, 0, 0,
                                    0xAA55))
                                self._add_tag(self._TAG_CLASS('Filedata', -1,
                                    FILEDATA_TAG, 1, 0, 255, 0, 0, 0,
                                    0xAA55))

                            self._parse_acqtbl(new_file)

                        elif new_file.name == M300_ACQUISITION_TABLE:
                            # If the tag class is still the "generic" SEATag,
                            # change it to the more specific version

                            if self._TAG_CLASS == tag.SEATag:
                                self._TAG_CLASS = tag.M300Tag
                                self._TIME_DATASET_NUMBER_OF_SAMPLES = 2
                                self._add_tag(self._TAG_CLASS('Time', TIME_TAG,
                                    self._TIME_DATASET_NUMBER_OF_SAMPLES, 1,
                                    self._TIME_DATASET_BYTES_PER_SAMPLE, 0, 0,
                                    0, 0, 'DummyTimeBoard', 0))
                                self._add_tag(self._TAG_CLASS("Filename",
                                    FILENAME_TAG, 1, 1, 0, 255, 0, 0, 0,
                                    'DummyFilenameBoard'))
                                self._add_tag(self._TAG_CLASS("Filedata",
                                    FILEDATA_TAG, 1, 1, 0, 255, 0, 0, 0,
                                    'DummyFiledataBoard'))

                            self._parse_acqtbl(new_file)

                        # TODO actually secondary acquisition data
                        elif new_file.name == M200_SECONDARY_ACQUISITION_TABLE or \
                                new_file.name == M300_SECONDARY_ACQUISITION_TABLE:
                            self._parse_acq2tbl(new_file)

                    else:
                        print('Found FILEDATA but no associated FILENAME.')

                elif dir_entry.tag_number == LAST_TAG:
                    break

        # We don't need to reread this file again
        self._already_initialized = True

    def _raw_buffers(self, filters={}):
        # Create a dummy instance of DirectoryEntry in order to find out what
        # attributes an instance of DirectoryEntry has.
        dummy_dir_entry = DirectoryEntry(*[None for i in range(10)])

        # Remove invalid filter criteria
        for attr_name in filters.keys():
            if not hasattr(dummy_dir_entry, attr_name):
                warnings.warn('Removing not-existent DiretoryEntry '
                                'attribute "{:s}" from filter '
                                'criteria'.format(attr_name))
                del filters[attr_name]

        cursor = 0
        search_time = 0
        prev_cursor = 0
        dir_base = cursor
        dir_entries = []

        # The filters argument is a whitelist, so if it is non-empty, the
        # default action is to reject buffers.
        if filters:
            drop_buffer = True
        else:
            drop_buffer = False
        reached_end = False
        has_filename = False
        first_tag = True

        pbar = tqdm(desc='Reading File',
                    total=len(self._get_contents()),
                    unit='bytes')

        # This while loop is effectively *THE* inner loop of this module. It is
        # not unusual for this loop to run millions of times, so avoid doing
        # anything unnecessary within it. If possible, do any checks,
        # comparisons, unchanging calculations, etc. before entering.
        while not reached_end:
            try:
                dir_entry = DirectoryEntry.new_from_raw_data(
                    self._get_contents(), cursor)
            except IncompleteDirectoryEntryException:
                raise UnexpectedEndOfFileException(
                        'Reached EOF while reading DirectoryEntry starting at '
                        'byte {cursor:d} ({cursor:#x}) of file {filename:s}'
                        ''.format(cursor=cursor, filename=self.filename))

            # Save the directory entries so that they can be returned with the
            # buffer, eliminating the need to read the directories again when
            # interpreting the data.
            dir_entries.append(dir_entry)
            if first_tag:
                if ((dir_entry.tag_number == TIME_TAG) &
                        (dir_entry.number_of_bytes == 36) &
                        (dir_entry.samples == 2) &
                        (dir_entry.bytes_per_sample == 18)):
                    first_tag = False
                else:
                    search_time += 1
                    cursor += 1
                    dir_base = cursor
                    dir_entries = []
            else:


                # This if statement contains clauses for all of the reserved tag
                # number specified in the unser manuals for the M200 and M300,
                # although not all of them are relevant in this context.
                if dir_entry.tag_number == TIME_TAG:
                    pass

                elif dir_entry.tag_number == NEXT_TAG:
                    cursor = dir_base + dir_entry.data_offset

                    if not drop_buffer:
                        yield dir_entries, self._get_contents()[dir_base:cursor]

                    dir_base = cursor
                    dir_entries = []
                    first_tag = True

                    # The filters argument is a whitelist, so if it is non-empty, the
                    # default action is to reject buffers.
                    if filters:
                        drop_buffer = True
                    else:
                        drop_buffer = False

                elif RESERVED_TAG_LOW_END <= dir_entry.tag_number <= \
                        RESERVED_TAG_HIGH_END:
                    # reserved for future use
                    pass

                elif dir_entry.tag_number == FILENAME_TAG:
                    pass

                elif dir_entry.tag_number == FILEDATA_TAG:
                    pass

                elif (dir_entry.tag_number == LAST_TAG):
                    yield dir_entries, self._get_contents()[dir_base:]
                    reached_end = True


                if filters:
                    for attr in filters.keys():
                        # No check error checking is done on getattr() here because
                        # we already verified that all filter criteria correspond
                        # to DirectoryEntry attributes at the beginning of this
                        # method.
                        #
                        # The pre-validation is done to avoid unnecesary if
                        # statements or try/catch blocks in this loop.
                        if getattr(dir_entry, attr) in filters[attr]:
                            drop_buffer = False
                            break

                # This is a separate 'if' statement from the 'elif' above
                if dir_entry.tag_number != NEXT_TAG:
                    cursor += DirectoryEntry.SIZE

            if cursor >= len(self._get_contents()) - dir_entry.SIZE:
                yield dir_entries, self._get_contents()[dir_base:]
                reached_end = True

            pbar.update(cursor - prev_cursor)
            prev_cursor = cursor

        pbar.close()
        print(search_time)

    def _parse_acqtbl(self, acqtbl):
        """Parses an acquisition table and saves data about about tags to expect
        in the rest of the file

        Arguments:
            acqtbl -- A ConfigFile containing the entire contents of acquisition
                table

        Returns:
            n/a

        """
        # remove lines that are comments
        data = acqtbl.data.decode('utf-8')
        lines = [l.replace('\t', ' ').rstrip() for l in data.split('\n')
                    if l[0] != ';']

        reader = csv.reader(lines, dialect=self._ACQ_CSV_DIALECT)
        tags = [self._TAG_CLASS(*row) for row in reader]

        # TODO reimplement buffer types

        for tag in tags:
            self._add_tag(tag)

    def _parse_acq2tbl(self, acq2tbl):
        """Parses secondary acquisition table

        Arguments:
            acq2tbl -- A ConfigFile containing the entire contents of
                a secondary acquisition table

        Returns:
            n/a

        Implemetation Details:
            As of the writing of this documentation, this method only secondary
            tag numbers so that other methods know which tags and buffers to
            ignore.

        """
        comment_start = re.compile("^;")
        lines = [l for l in acq2tbl.data.split('\n')
                    if not comment_start.match(l)]

        secondary_tags = [int(l.split()[1]) for l in lines]

        self._secondary_tags.update(secondary_tags)

    def _unpack_string_data(self, raw_buffer, dir_entry):
        """Unpacks strings from a buffer.

        Unpacks a number of consecutive strings of a given length from a buffer
        of raw data.

        Arguments:
            raw_data -- a buffer of data from an M200 data file which contains
                the strings that need to be read
            dir_entry -- a DirectoryEntry with metadata pertaining to the string
                data

        Returns:
            A tuple of the strings that were read from the buffer

        """
        format_string = '<{:d}s'.format(dir_entry.bytes_per_sample) * \
            dir_entry.samples
        return struct.unpack_from(
            format_string, raw_buffer, dir_entry.data_offset)

    def iter_buffers(self, filters={}):
        """Iterates over each buffer in self.

        Arguments:
            n/a

        Returns:
            A generator that will iterate over each buffer in self.

        Warns:
            SEAReadWarning -- Warns if something unexpected/unsupported is
            found while reading through self.

        Generator:
            Yields:
                Each buffer of self, in order.

            Raises:
                StopIteration -- It's what iterators and generators do.

        """
        for dir_entries, raw_buffer in self._raw_buffers(filters):
            new_buffer = Buffer()
            if filters:
                discard_buffer = True
            else:
                discard_buffer = False

            for dir_entry in dir_entries:
                unknown_type = False

                if dir_entry.tag_number == TIME_TAG:
                    data = SEATime._make(
                        SEA_TIME_STRUCT.unpack_from(
                            raw_buffer, dir_entry.data_offset))

                    new_buffer.add_dataset(dir_entry, data)

                elif dir_entry.tag_number == NEXT_TAG:
                    if not discard_buffer:
                        # TODO reimplement buffer types
                        # # find which buffer type has the same set of tags as the
                        # # new buffer
                        # new_buffer_counter = collections.Counter(
                        #   [tn for tn in new_buffer.get_tag_numbers()
                        #       if tn not in [TIME_TAG, NEXT_TAG, LAST_TAG]
                        #       ])

                        # for buffer_type in self._buffer_types:
                        #   if new_buffer_counter == buffer_type.counter:
                        #       new_buffer_type = buffer_type
                        #       break
                        # else:
                        #   new_buffer_type = -1

                        # new_buffer.set_type(new_buffer_type)

                        # This 'yield' function makes this method a generator
                        # (google it). It allows us to only have one buffer
                        # object in memory at a time (versus creating a list of
                        # every buffer object and returning it at the end.)
                        yield new_buffer

                    break

                elif RESERVED_TAG_LOW_END <= dir_entry.tag_number <= \
                        RESERVED_TAG_HIGH_END:
                    # reserved for future use
                    warnings.warn('Found reserved tag '
                                    'number {:d}'.format(dir_entry.tag_number),
                                    SEAReadWarning)

                elif dir_entry.tag_number == FILENAME_TAG:
                    new_buffer.add_dataset(dir_entry,
                            self._unpack_string_data(raw_buffer, dir_entry))

                elif dir_entry.tag_number == FILEDATA_TAG:
                    new_buffer.add_dataset(dir_entry,
                            self._unpack_string_data(raw_buffer, dir_entry))

                elif dir_entry.tag_number == COMMAND_TAG:
                    discard_buffer = True
                elif dir_entry.tag_number == ERROR_TAG:
                    discard_buffer = True
                elif dir_entry.tag_number == SAME_TAG:
                    discard_buffer = True

                elif dir_entry.tag_number == LAST_TAG:
                    # We no longer need that reference to the binary file data
                    self._file_contents = None
                    # Reset the filters parameter since using mutable objects
                    # as default parameters can have some unexpected side
                    # effects
                    filters={}
                    # There are no more buffers to iterate over
                    return

                elif dir_entry.tag_number in self._secondary_tags:
                    # I haven't decided how to handle the secondary
                    # acquisitions, yet.
                    discard_buffer = True
                elif filters:
                    for attr in filters.keys():
                        if getattr(dir_entry, attr) in filters[attr]:
                            dataset = self._read_dataset(dir_entry, raw_buffer)
                            new_buffer.add_dataset(dir_entry, dataset)
                            discard_buffer = False
                else:
                    dataset = self._read_dataset(dir_entry, raw_buffer)

                    new_buffer.add_dataset(dir_entry, dataset)


    def _read_dataset_raw(self, dir_entry, raw_buffer, dataset_string):
        try:
            # The '<' at the start of the format string tells python
            # to read the data in little endian byte order.
            dataset = struct.unpack_from(
                '<{:s}'.format(dataset_string), raw_buffer,
                    dir_entry.data_offset)
        except Exception as e:
            print("Tag Number: {:d}\nAcq type: {:d}\n"
                    "Data Offset: {:d}\n# Samples: {:d}\n"
                    "B/Sample: {:d}\nStruct Size: {:d}\n"
                    "format: {:s}\n".format(
                        dir_entry.tag_number, dir_entry.typ,
                        dir_entry.data_offset, dir_entry.samples,
                        dir_entry.bytes_per_sample,
                        struct.calcsize(dataset_string),
                        's'))
            raise e

        return dataset

    def _read_dataset(self, dir_entry, raw_buffer):
        # Determine what the struct format string for this dataset
        # is. Most of the comments below are borrowed from the SEA
        # Model 200 User's Manual (ie m200.pdf).
        # Model 300 User's Manual (ie M300Reference.pdf).
        # ref: http://docs.python.org/2/library/struct.html
        # ref: m200.pdf
        # ref: M300Reference.pdf
        #
        # b - 8-bit signed integer
        # B - 8-bit unsigned integer
        # h - 16-bit signed integer
        # H - 16-bit unsigned integer
        # i - 32-bit signed integer
        # I - 32-bit unsigned integer
        # Q - 64-bit unsigned integer
        # Xs - a string of length X

        unknown_type = False

        if dir_entry.typ == 1:
            # Acquisition Type 1 (CAMAC Analog E205/E210)
            # 16-bit, two's complement integer
            sample_string = 'h'

        elif dir_entry.typ == 2:
            # Aquisition Type 2 (CAMAC 1D Counts)
            # 42 bytes representing 15 16-bit numbers followed by 2
            # 8-bit numbers. Since the 16-bit values are counts,
            # they should be unsigned.
            sample_string = '{:s}bb'.format('H' * 20)

        elif dir_entry.typ == 5:
            # Acquisition Type 5 (2D Mono Image)
            # This routine acquires the 4096 byte image block of the
            # 2D Mono probe. Each image contains 1024 32 bit slices.
            # Allocate 4096 bytes for this acquisition.
            sample_string = 'I' * 1024

        elif dir_entry.typ == 6:
            # Acquisition Type 6 (2D Mono TAS Factors)
            # This routine acquires two 16 bit words.
            sample_string = 'HH'

        elif dir_entry.typ == 7:
            # Acquisition Type 7 (2D Mono Elapsed Time)
            # This routine acquires a 32 bit word.
            sample_string = 'I'

        elif dir_entry.typ == 8:
            # Acquisition Type 8 (2D Mono Elapsed TAS/100)
            # This routine acquires a 32 bit word.
            sample_string = 'I'

        elif dir_entry.typ == 9:
            # Acquisition Type 9 (2D Mono Elapsed Shadow OR)
            sample_string = 'H'

        elif dir_entry.typ == 10:
            # Acquisiiton Type 10 (2D Mono Total Shadow OR)
            # This routine acquires a 16-bit, unsigned word.
            sample_string = 'H'

        elif dir_entry.typ == 11:
            # Acquisition Type 11 (2D Mono House Data)
            # This routine acquires 8 16-bit words.
            sample_string = 'H' * 8

        elif dir_entry.typ == 35:
            # Acquisition Type 35 (SEA Analog to Digital Input)
            # This rotine acquires two 8-bit bytes. The data acquired is in
            # two's complement integer coding.
            sample_string = 'h'

        elif dir_entry.typ == 66:
            # Acquisition Type 66 (2D Grey Advanced)
            # The final data size will not be larger than the number
            # of samples times the bytes per sample.
            # Each sample should be a set of 128-bit slices
            if dir_entry.bytes_per_sample % 16 != 0:
                warnings.warn(
                        '2D Grey Advanced sample size ({:d} bytes) '
                        'is not a multiple of 128 bits. The '
                        'resulting slices may me incorrect, '
                        'incomplete, misaligned, or otherwise '
                        'corrupt.'.format(
                            dir_entry.bytes_per_sample))

            num_slices = dir_entry.bytes_per_sample / 16
            # TODO The first 64-bit number will be
            try:
                sample_string = 'QQ' * num_slices
            except TypeError:
                sample_string = 'QQ' * math.ceil(num_slices)
                print(num_slices)

        elif dir_entry.typ == 78:
            # Acquisition Type 78 (CIP Image Data)
            # This data is stored in a compressed format, so just
            # read it as a bunch of bytes.
            sample_string = '{}H'.format('B' * 4096)

        elif dir_entry.typ == 85:
            # Acquisition Type 85 (Network Binary Data)
            # This is not necessarily stored in any particular format, so read
            # it as a bunch of bytes and let the receviver decode the data.
            sample_string = 'B' * dir_entry.bytes_per_sample

        elif dir_entry.typ == 255:
            # Acquisition Type 255 (Dummy Acquisition)
            sample_string = 'B' * dir_entry.bytes_per_sample

        else:
            # Unknown acquisition type, read it in as bytes
            warnings.warn(
                'Unknown acquisition type ({acq_type:d}) will be '
                'read as list of bytes. If you need to read data '
                'with acquisition type {acq_type:d}, you should '
                'update this program to recognize '
                'it.'.format(acq_type=dir_entry.typ),
                SEAReadWarning)

            dataset_string = 'B' * dir_entry.number_of_bytes
            unknown_type = True

        if not unknown_type:
            expected_size = dir_entry.samples * \
                    dir_entry.bytes_per_sample

            if dir_entry.number_of_bytes < expected_size:
                warnings.warn(
                    'Number of bytes in buffer ({:d}) is less than '
                    'expected ({:d}). This is legal in the file '
                    'format, but is not currently supported by this '
                    'program.'.format(
                        dir_entry.number_of_bytes, expected_size),
                    SEAReadWarning)
            elif dir_entry.data_offset + expected_size > len(raw_buffer):
                # If a dataset apparently goes past the end of the
                # buffer, raise an error.
                print(dir_entry.number_of_bytes, dir_entry.typ, dir_entry.samples, dir_entry.bytes_per_sample, expected_size, dir_entry.data_offset)
                warnings.warn(
                        'Expected end of dataset (0x{:x}) is beyond end '
                        'of buffer (0x{:x}).'.format(
                            dir_entry.data_offset + \
                                    expected_size, len(raw_buffer))
                        )
                raise ValueError
                return []


            dataset_string = sample_string * dir_entry.samples

        return self._read_dataset_raw(dir_entry, raw_buffer, dataset_string)

class BufferType(object):
    def __init__(self, number, tags):
        self.number = number
        self.tags = tuple(tags)
        self.counter = collections.Counter(
                                [t.tag_number for t in self.tags])


    def __hash__(self):
        return self.number

    def __cmp__(self, other):
        return self.number - other.number

    def make_buffer(self):
        buff = Buffer()

        buff.number = self.number
        buff.tags = self.tags

        return buff


class Buffer(object):
    """Software representaion of a single buffer in an M200 file

    Methods:
        set_type -- set the BufferType of this Buffer
        get_type -- get the BufferType of this Buffer
        add_dataset -- add a dataset from a buffer in an M200 file to this
            Buffer
        get_dir_entry_by_tag_number -- fetch a DirectoryEntry from this Buffer
        get_tag_numbers -- get a list of tag numbers for all DirectoryEntries
            associated with this Buffer
        get_dir_entries_by_typ -- get a list of all DirectoryEntries in this
            Buffer with a given typ(e)
        get_dataset_by_tag_number -- get the dataset from this Buffer that has a
            given tag number
        get_datasets_by_typ -- get a list of all datasets in this Buffer that
            have a given typ(e)

    """
    def __init__(self):
        # Since Python uses object references (ie won't consume
        # memory with a bajillion copies) and since the buffer will
        # be queried based on multiple criteria, we will use a number
        # of dicts in order to provide lookups with complexity O(1).
        self.dir_entries_by_tag_number = {}
        self.dir_entries_by_typ = {}
        self.datasets_by_tag_number = {}
        self.datasets_by_typ = {}

        self.buffer_type = None

    def set_type(self, buffer_type):
        self.buffer_type = buffer_type

    def get_type(self):
        return self.buffer_type

    def add_dataset(self, dir_entry, dataset):
        self.dir_entries_by_tag_number[dir_entry.tag_number] = dir_entry
        self.datasets_by_tag_number[dir_entry.tag_number] = dataset

        # In diretory entries for time data in asynchronous buffers, the
        # acquisition type (apparently) contains the acquisition type of the
        # buffer's asynchronous master event, rather than the acquisition type
        # of the time. To prevent this from interfering with retrieval of data
        # by type, we will not store time data in the dicts which are indexed by
        # acquisition type.
        #
        # Since there should only ever be a single time dataset in a buffer,
        # retrieval by tag number is still possible (and makes sense) for time
        # data, especially since time data will always have the same tag number.
        if dir_entry.tag_number != TIME_TAG:
            try:
                self.dir_entries_by_typ[dir_entry.typ].append(dir_entry)
            except KeyError as ke:
                if ke.args[0] == dir_entry.typ:
                    self.dir_entries_by_typ[dir_entry.typ] = [dir_entry]
                else:
                    raise ke

            try:
                self.datasets_by_typ[dir_entry.typ].append(dataset)
            except KeyError as ke:
                if ke.args[0] == dir_entry.typ:
                    self.datasets_by_typ[dir_entry.typ] = [dataset]
                else:
                    raise ke

    def get_dir_entry_by_tag_number(self, tag_number):
        return self.dir_entries_by_tag_number[tag_number]

    def get_tag_numbers(self):
        return self.dir_entries_by_tag_number.keys()

    def get_dir_entries_by_typ(self, typ):
        return self.dir_entries_by_typ[typ]

    def get_dataset_by_tag_number(self, tag_number):
        return self.datasets_by_tag_number[tag_number]

    def get_datasets_by_typ(self, typ):
        return self.datasets_by_typ[typ]

    def __str__(self):
        strings = []
        strings.append('Buffer (Type {})'.format(self.buffer_type.number
            if self.buffer_type is not None else 'unknown'))

        for dir_entry in self.dir_entries_by_tag_number.values():
            strings.append(str(dir_entry))

        return '\n\t'.join(strings)


class DirectoryEntry(object):
    struct = struct.Struct('<HHHHHBBBBH')
    SIZE = 16

    def __init__(self, tag_number, data_offset, number_of_bytes, samples,
            bytes_per_sample, typ, parameter1, parameter2, parameter3, address):
        self.tag_number = tag_number
        self.data_offset = data_offset
        self.number_of_bytes = number_of_bytes
        self.samples = samples
        self.bytes_per_sample = bytes_per_sample
        self.typ = typ
        self.parameter1 = parameter1
        self.parameter2 = parameter2
        self.parameter3 = parameter3
        self.address = address

    def __repr__(self):
        return ("<sea.DirectoryEntry tag_number={tag_number:d} "
            "data_offset={data_offset:d} number_of_bytes={number_of_bytes:d} "
            "samples={samples:d} bytes_per_sample={bytes_per_sample:d} "
            "type={typ:d} parameter1={parameter1:d} parameter2={parameter2:d} "
            "parameter3={parameter3:d} address={address:#x}"
            "".format(**vars(self)))

    @classmethod
    def new_from_raw_data(cls, file_contents, start=0):
        try:
            return cls(*cls.struct.unpack_from(file_contents, start))
        except struct.error as se:
            if str(se) == 'unpack_from requires a buffer of at least 16 bytes':
                raise IncompleteDirectoryEntryException
            else:
                raise se


class ConfigFile(object):
    def __init__(self, name=None, data=None):
        self.name = name
        self.data = data
