from re import search, IGNORECASE, compile
from ._util import fetch_file, flat_extract, permissive_search_group
from ._checks import safe_file_name
from ._exceptions import GeneLabJSONException
from pandas import concat, Series, Index, read_csv, DataFrame
from pandas.errors import ParserError
from collections import defaultdict
from numpy import nan
from os import walk
from os.path import join


class PerSampleMatrix(DataFrame): pass
class DataMatrix(DataFrame): pass
class CompoundMatrix(DataFrame): pass


class AssayMetadataLocator():
    """Emulate behavior of Pandas `.loc` for class Assay()"""

    def __init__(self, parent):
        """Point to parent"""
        self.parent = parent

    def __getitem__(self, key):
        """Query parent.metadata with .loc, using field titles instead of internal field ids"""
        if isinstance(key, tuple): # called with .loc[x, y]
            try:
                indices, titles = key
            except ValueError:
                raise IndexError("Incorrect index for assay metadata")
            subset = self.parent[titles].loc[indices]
        else: # assume called with .loc[x] and interpret `x` the best we can
            subset = self.parent.metadata.loc[key]
        if (subset.shape == (1,)) and (not self.parent.strict_indexing):
            return subset.iloc[0]
        else:
            return subset


class Assay():
    """Stores individual assay metadata"""
    name = None
    parent = None
    metadata = None
    glds_file_urls = None
    fields = None
    strict_indexing = True
    storage = None

    def __init__(self, parent, name, json, glds_file_urls, storage_prefix, strict_indexing=True, **kwargs):
        """Parse JSON into assay metadata"""
        self.parent = parent
        self.name = name
        self.glds_file_urls = glds_file_urls
        self.storage = join(storage_prefix, name)
        self._json = json
        self._raw, self._header = self._json["raw"], self._json["header"]
        # populate and freeze self.fields (this can be refactored...):
        self._field2title = {
            entry["field"]: entry["title"] for entry in self._header
        }
        if len(self._field2title) != len(self._header):
            raise GeneLabJSONException("Conflicting IDs of data fields")
        self.fields = defaultdict(set)
        for field, title in self._field2title.items():
            self.fields[title].add(field)
        self.fields = dict(self.fields)
        # populate metadata and index with Sample Name:
        self.metadata = concat(map(Series, self._raw), axis=1).T
        if len(self.fields["Sample Name"]) != 1:
            raise GeneLabJSONException("Number of 'Sample Name' fields != 1")
        else:
            sample_name_field = list(self.fields["Sample Name"])[0]
            self.metadata = self.metadata.set_index(sample_name_field)
        # initialize indexing functions:
        self.strict_indexing = strict_indexing
        self.loc = AssayMetadataLocator(self)

    def __getitem__(self, titles):
        """Get metadata by field title (rather than internal field id)"""
        if isinstance(titles, (tuple, list, Series, Index)):
            if isinstance(titles, Series) and (titles.dtype == bool):
                return self.metadata.loc[titles]
            else:
                return self.metadata[
                    list(set.union(*[self.fields[t] for t in titles]))
                ]
        elif self.strict_indexing:
            raise IndexError("Assay: column indexer must be list-like")
        else:
            subset = self.metadata[list(self.fields[titles])]
            if subset.shape[1] > 1:
                return subset
            else:
                return subset.iloc[:,0]

    @property
    def has_arrays(self):
        """Boolean flag, True if has 'Array Design REF', False otherwise"""
        return ("Array Design REF" in self.fields)

    @property
    def available_file_types(self):
        """List file types referenced in metadata"""
        file_types = set()
        for title in self.fields:
            if search(r'\bfile\b', title, flags=IGNORECASE):
                available_files = self[[title]].values.flatten()
                if not set(available_files) <= {"", None, nan}:
                    file_types.add(title)
        return file_types

    @property
    def available_derived_file_types(self):
        """List file types with derived data referenced in metadata"""
        return {
            permissive_search_group(r'^.*(processed|derived).+file.*$', ft)
            for ft in self.available_file_types
        } - {None}

    @property
    def available_derived_matrix_file_types(self):
        """List file types with derived data referenced in metadata"""
        return {
            permissive_search_group(r'^.*(processed|derived).+matrix.+file.*$', ft)
            for ft in self.available_file_types
        } - {None}

    @property
    def available_protocols(self):
        """List protocol REFs referenced in metadata"""
        if "Protocol REF" in self.fields:
            return (
                set(self[["Protocol REF"]].values.flatten()) -
                {"", None, nan}
            )
        else:
            return set()

    def _get_file_url(self, filemask):
        """Get URL of file defined by file mask (such as *SRR1781971_*)"""
        regex_filemask = filemask.split("/")[0].replace("*", ".*")
        matching_names = [
            filename for filename in self.glds_file_urls.keys()
            if search(regex_filemask, filename)
        ]
        if len(matching_names) == 0:
            return None
        elif len(matching_names) > 1:
            raise GeneLabJSONException("Multiple file URLs match name")
        else:
            return self.glds_file_urls[matching_names[0]]

    def _download_archive_files(self, force_reload=False):
        """Download files/archives etc that contain files targeted by get_combined_matrix()"""
        derived_entries = self[list(self.available_derived_file_types)]
        internal_file_urls = set()
        for field in derived_entries.columns:
            internal_file_urls |= set(map(
                self._get_file_url, derived_entries[field].values
            ))
        external_file_urls = set(filter(
            compile(r'^(ftp|http|https):\/\/').search,
            derived_entries.values.flatten()
        ))
        file_urls = (internal_file_urls | external_file_urls) - {None}
        if not file_urls:
            raise GeneLabJSONException("Nothing to download")
        for file_url in file_urls:
            file_name = safe_file_name(file_url)
            fetch_file(file_name, file_url, self.storage, update=force_reload)

    def _extract_archive_files(self):
        """Extract all files in current assay's storage"""
        for filename in next(walk(self.storage))[2]:
            if filename.endswith(".zip"):
                flat_extract(
                    join(self.storage, filename),
                    target_directory=self.storage
                )

    def _matrix_from_samples(self, derived_files):
        """Make combined matrix from individually referenced derived data files"""
        sample_dataframes = []
        for sample_name, derived_filename in derived_files.iteritems():
            filename = safe_file_name(
                derived_filename, as_mask=True, directory=self.storage
            )
            sample_dataframe = read_csv(
                join(self.storage, filename), sep="\t", comment="#"
            )
            sample_dataframe["Sample Name"] = sample_name
            sample_dataframes.append(sample_dataframe)
        return PerSampleMatrix(
            concat(sample_dataframes, axis=0, ignore_index=True)
        )

    def _matrix_from_datamatrix(self, derived_files):
        """Assuming a single data matrix referenced in metadata, try parsing to expected structure"""
        derived_files_set = set(derived_files)
        if len(derived_files_set) == 1:
            filename = safe_file_name(
                derived_files_set.pop(), as_mask=True, directory=self.storage
            )
            return DataMatrix(
                read_csv(
                    join(self.storage, filename), sep="\t", comment="#",
                    header=[0, 1], index_col=0
                )
            )
        else:
            raise NotImplementedError("Multiple data matrices referenced")

    def get_combined_matrix(self, force_reload=False):
        """Download (if necessary), extract, parse and combine derived files"""
        self._download_archive_files(force_reload=force_reload)
        self._extract_archive_files()
        derived_entries = self[list(self.available_derived_file_types)]
        for field in derived_entries.columns:
            if len(set(derived_entries[field])) == derived_entries.shape[0]:
                derived_files = derived_entries[field]
                try:
                    matrix = self._matrix_from_samples(derived_files)
                except ParserError:
                    pass
                else:
                    return matrix
        if self.available_derived_matrix_file_types:
            derived_entries = self[
                list(self.available_derived_matrix_file_types)
            ]
            for field in derived_entries.columns:
                try:
                    matrix = self._matrix_from_datamatrix(
                        derived_entries[field]
                    )
                except (ParserError, NotImplementedError):
                    pass
                else:
                    return matrix
        raise NotImplementedError(
            "Derived files not referenced individually or as single data matrix"
        )