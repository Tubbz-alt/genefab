from os.path import join
from ._exceptions import GeneLabJSONException, GeneLabFileException
from ._exceptions import GeneLabException
from collections import defaultdict
from pandas import concat, Series, Index, DataFrame, read_csv, merge
from re import search, fullmatch, split, IGNORECASE, sub
from numpy import nan
from ._util import fetch_file, DELIM_AS_IS, to_cls
from copy import deepcopy

ASSAY_CHARACTERISTICS = [
    "normalized annotated data file",
    "differential expression analysis data transformation",
    "normalized counts data file"
]
ASSAY_PROTOCOL_LIST = [
    "genelab microarray data processing protocol",
    "genelab rnaseq data processing protocol"
]


class MetadataRow():
    """Implements a slice of assay metadata for one sample, Series-like"""

    def __init__(self, parent, sample, raw_row):
        """Inherit from parent(s)"""
        self.parent = parent
        self.sample = sample
        self.raw_row = raw_row.copy()

    def __getitem__(self, key):
        """Reuse parent methods"""
        if isinstance(key, str):
            return self.parent.metadata.loc[self.sample, [key]].iloc[0]
        else:
            return self.parent.metadata.loc[self.sample, key]

    def __repr__(self):
        """Short description of fields and samples"""
        return "\n".join([
            "index: " + self.sample,
            "fields: [" + ", ".join(
                repr(k) for k in self.parent._fields.keys()
            ) + "]"
        ])


class AssayMetadataLocator():
    """Emulate behavior of Pandas `.loc` for class AssayMetadata()"""

    def __init__(self, parent):
        """Point to parent"""
        self.parent = parent

    def __getitem__(self, key):
        """Query parent.raw_metadata with .loc, using field titles instead of internal field ids"""
        if isinstance(key, tuple): # called with .loc[x, y]
            try:
                index_patterns, title_patterns = key
            except ValueError:
                raise IndexError("Incorrect index for assay metadata")
            else: # assume both indices and titles are collections of regexes
                indices = set.union(*({
                        ix for ix in self.parent.parent.raw_metadata.index
                        if fullmatch(pattern, ix, flags=IGNORECASE)
                    } for pattern in index_patterns
                ))
                row_subset = self.parent.loc[list(indices)]
                field_titles = set.union(*(
                    self.parent.parent._match_field_titles(t, method=fullmatch)
                    for t in title_patterns
                ))
                fields = set.union(*(
                    self.parent.parent._fields[title]
                    for title in field_titles
                ))
                return row_subset[list(fields)]
        else: # assume called with .loc[x] and interpret `x` the best we can
            if isinstance(key, DataFrame) and (key.shape[1] == 1):
                # assume being indexed by boolean column, delegate to parent[]:
                return self.parent[key]
            elif isinstance(key, (tuple, list, set)):
                # assume it is a collection of regexes:
                indices = set.union(*({
                        ix for ix in self.parent.parent.raw_metadata.index
                        if fullmatch(pattern, ix, flags=IGNORECASE)
                    } for pattern in key
                ))
                return self.parent.parent.raw_metadata.loc[list(indices)]
            else: # last resort; just pass it to raw_metadata directly
                return self.parent.parent.raw_metadata.loc[key]


class AssayMetadata():
    """Makes individual assay metadata accessible with Pandas-like indexing"""

    def __init__(self, parent):
        """Point to parent and initialize children"""
        self.parent = parent
        self.loc = AssayMetadataLocator(self)

    def to_frame(self):
        """Raw metadata with multiindex columns (human-readable -> internal)"""
        multicols = ["field", "internal_field"]
        fields_df = DataFrame(
            data=[[k, v] for k, vv in self.parent._fields.items() for v in vv],
            columns=multicols
        )
        columns_df = DataFrame(
            data=self.parent.raw_metadata.columns, columns=["internal_field"]
        )
        multiindex_df = merge(columns_df, fields_df, sort=False, how="outer") \
            .fillna("Unknown")
        mdv = multiindex_df["internal_field"].values
        rmv = self.parent.raw_metadata.columns.values
        if (mdv != rmv).any():
            raise GeneLabException("Could not generate extended raw metadata")
        as_frame = self.parent.raw_metadata.copy()
        as_frame.columns = multiindex_df.set_index(multicols).index
        return as_frame.sort_index(by="field", axis="columns")

    def __repr__(self):
        """Use the repr of the dataframe form"""
        return repr(self.to_frame())

    def __getitem__(self, patterns):
        """Get metadata by field title (rather than internal field id)"""
        if isinstance(patterns, DataFrame) and (patterns.shape[1] == 1):
            # assume being indexed by boolean column, check if able to coerce:
            indexer = patterns.iloc[:,0]
            if indexer.dtype == bool:
                return self.parent.raw_metadata.loc[indexer]
            else:
                raise IndexError("Cannot index by arbitrary DataFrame")
        if isinstance(patterns, (tuple, list, set, Series, Index)):
            # assume being indexed by column name regex:
            titles = set.union(set(), *(
                self.parent._match_field_titles(p, method=fullmatch)
                for p in patterns
            ))
            if titles:
                return self.parent.raw_metadata[
                    list(set.union(*(self.parent._fields[t] for t in titles)))
                ]
            else:
                return DataFrame()
        else:
            raise IndexError("AssayMetadata: column indexer must be list-like")

    def iterrows(self):
        """Iterate over metadata slices for each sample"""
        for sample, raw_row in self.parent.raw_metadata.iterrows():
            yield sample, MetadataRow(self.parent, sample, raw_row)

    @property
    def index(self):
        """List of samples"""
        return self.parent.raw_metadata.index

    @property
    def fields(self):
        """Alias to self.parent._fields"""
        return self.parent._fields

    @property
    def columns(self):
        """List of full-length column indexing options"""
        return list(self.parent._fields.keys())


class Assay():
    """Stores individual assay information and metadata in raw form"""
    name = None
    _fields, raw_metadata, metadata = None, None, None
    parent, glds_file_urls = None, None
    storage = None
    _normalized_data, _processed_data = None, None
    _indexed_by, _name_delim, _field_indexed_by = None, True, None

    def __init__(self, parent, name, json, glds_file_urls, storage_prefix, index_by, name_delim):
        """Parse JSON into assay metadata"""
        self.parent, self.name, self._json = parent, name, json
        self.glds_file_urls = glds_file_urls
        self.storage = join(storage_prefix, name)
        self._raw, self._header = self._json["raw"], self._json["header"]
        # populate and freeze self._fields (this can be refactored...):
        self._field2title = {e["field"]: e["title"] for e in self._header}
        if len(self._field2title) != len(self._header):
            raise GeneLabJSONException("Conflicting IDs of data fields")
        self._fields = defaultdict(set)
        for field, title in self._field2title.items():
            self._fields[title].add(field)
        self._fields = dict(self._fields)
        # populate metadata and index with `index_by`:
        self.raw_metadata = concat(map(Series, self._raw), axis=1).T
        self._field_indexed_by = self._get_unique_field_from_title(index_by)
        maybe_indexed_by = self._match_field_titles(index_by, method=fullmatch)
        if len(maybe_indexed_by) != 1:
            raise IndexError(
                "Nonexistent or ambiguous index_by value: '{}'".format(index_by)
            )
        self._indexed_by = maybe_indexed_by.pop()
        self.raw_metadata = self.raw_metadata.set_index(self._field_indexed_by)
        self._name_delim = name_delim
        if name_delim != DELIM_AS_IS:
            self.raw_metadata.index = self.raw_metadata.index.map(
                lambda f: sub(r'[._-]', name_delim, f)
            )
        del self._fields[self._indexed_by]
        # initialize indexing functions:
        self.metadata = AssayMetadata(self)

    def __repr__(self):
        """Condensed representation"""
        return "\n".join([
            "name: " + self.name,
            "samples: [" + ", ".join(
                repr(ix) for ix in self.raw_metadata.index
            ) + "]",
            "factor values: " + repr(self.factor_values)
        ])

    def _match_field_titles(self, pattern, flags=IGNORECASE, method=search):
        """Find fields matching pattern"""
        if self._indexed_by:
            field_pool = set(self._fields) | {self._indexed_by}
        else:
            field_pool = self._fields
        return {
            title for title in field_pool
            if method(pattern, title, flags=flags)
        }

    def _get_unique_field_from_title(self, title):
        """Get unique raw metadata column name; fail if anything is ambiguous"""
        matching_titles = self._match_field_titles(title)
        if len(matching_titles) == 0:
            raise IndexError("Nonexistent '{}'".format(title))
        elif len(matching_titles) > 1:
            raise IndexError("Ambiguous '{}'".format(title))
        else:
            matching_title = matching_titles.pop()
            if matching_title == self._indexed_by:
                matching_fields = {self._field_indexed_by}
            else:
                matching_fields = self._fields[matching_title]
        if len(matching_fields) == 0:
            raise IndexError("Nonexistent '{}'".format(title))
        elif len(matching_fields) > 1:
            raise IndexError("Ambiguous '{}'".format(title))
        else:
            return list(matching_fields)[0]

    @property
    def factor_values(self):
        """Get factor names and their values"""
        return {
            field_title: set(self.metadata[[field_title]].values.flatten())
            for field_title in self._match_field_titles(r'^factor value:  ')
        }

    def annotation(self, differential_annotation=True, named_only=True, index_by="Sample Name", cls=None, continuous="infer"):
        """Get annotation of samples: entries that differ (default) or all entries"""
        samples_keys = set(self.parent.samples.keys())
        if len(samples_keys) == 1:
            samples_key = samples_keys.pop()
        else:
            samples_key = sub(r'^a', "s", self.name)
        if samples_key not in self.parent.samples:
            error_message = "Could not find an unambiguous samples key"
            raise GeneLabJSONException(error_message)
        annotation_dataframe = concat([
            Series(raw_sample_annotation)
            for raw_sample_annotation in self.parent.samples[samples_key]["raw"]
        ], axis=1)
        samples_field2title = {
            entry["field"]: entry["title"]
            for entry in self.parent.samples[samples_key]["header"]
        }
        if named_only:
            index_subset = [
                field for field in annotation_dataframe.index
                if field in samples_field2title
            ]
            annotation_dataframe = annotation_dataframe.loc[index_subset]
        annotation_dataframe.index = annotation_dataframe.index.map(
            lambda field: samples_field2title.get(field, field)
        )
        if differential_annotation:
            differential_rows = annotation_dataframe.apply(
                lambda r: len(set(r.values))>1, axis=1
            )
            annotation_dataframe = annotation_dataframe[differential_rows]
        annotation_dataframe = annotation_dataframe.T.set_index(index_by).T
        if self._name_delim != DELIM_AS_IS:
            annotation_dataframe.columns = annotation_dataframe.columns.map(
                lambda f: sub(r'[._-]', self._name_delim, f)
            )
        annotation_dataframe.columns.name = index_by
        if cls:
            return to_cls(
                annotation_dataframe.T, target=cls, continuous=continuous
            )
        else:
            return annotation_dataframe.T

    def factors(self, cls=None, continuous="infer"):
        """Get DataFrame of samples and factors in human-readable form"""
        annotation = self.annotation()
        factor_fields = [
            field for field in annotation.columns
            if search(r'^factor value', field, flags=IGNORECASE)
        ]
        factors_dataframe = annotation[factor_fields]
        factors_dataframe.index.name, factors_dataframe.columns.name = (
            self._indexed_by, "Factor"
        )
        if cls == "*":
            if factors_dataframe.shape[1] != 1:
                raise KeyError("one of multiple factors needs to be specified")
            else:
                cls = str(factors_dataframe.columns[0])
            return to_cls(factors_dataframe, target=cls, continuous=continuous)
        elif cls is not None:
            return to_cls(factors_dataframe, target=cls, continuous=continuous)
        else:
            return factors_dataframe

    @property
    def has_arrays(self):
        return "Array Design REF" in self._fields

    @property
    def has_normalized_data(self):
        return len(self._match_field_titles("normalized data files")) > 0

    @property
    def has_processed_data(self):
        return (
            len(self._match_field_titles("normalized annotated data files")) > 0
        )

    # alias:
    @property
    def has_normalized_annotated_data(self):
        return self.has_processed_data

    @property
    def available_file_types(self):
        """List file types referenced in metadata"""
        file_types = set()
        for title in self._match_field_titles(r'\bfile\b'):
            available_files = self.metadata[[title]].values.flatten()
            if not set(available_files) <= {"", None, nan}:
                file_types.add(title)
        return file_types

    def _get_file_url(self, filemask):
        """Get URL of file defined by file mask (such as *SRR1781971_*)"""
        regex_filemask = filemask.split("/")[0].replace("*", ".*")
        matching_names = {
            filename for filename in self.glds_file_urls.keys()
            if search(regex_filemask, filename)
        }
        if len(matching_names) == 0:
            return None
        elif len(matching_names) > 1:
            raise GeneLabJSONException("Multiple file URLs match name")
        else:
            return self.glds_file_urls[matching_names.pop()]

    def _translate_data_sample_names(self, data, data_columns):
        """Convert data header to match metadata index"""
        field_from = self._get_unique_field_from_title(data_columns)
        field_to = self._field_indexed_by
        column_from, column_to = (
            self.raw_metadata.reset_index()[field_from],
            self.raw_metadata.reset_index()[field_to]
        )
        if len(column_from) == len(set(column_from)) == len(set(column_to)):
            column_translator = dict(zip(column_from, column_to))
        else:
            raise IndexError("Cannot reindex '{}' to ambiguous '{}'".format(
                self._indexed_by, data_columns
            ))
        translated_data = data.copy()
        translated_columns = []
        for column in data.columns:
            matching_keys = {
                k for k in column_translator.keys() if search(k, column)
            }
            if len(matching_keys) == 1:
                translated_columns.append(
                    column_translator[matching_keys.pop()]
                )
            else:
                raise IndexError("Cannot reindex '{}' to ambiguous '{}'".format(
                    self._indexed_by, data_columns
                ))
        translated_data.columns = Index(
            translated_columns, name=data.columns.name
        )
        return translated_data

    def _read_data_from(self, field_title, blacklist_regex, force_redownload, translate_sample_names, data_columns, sep="\t"):
        """Download (if necessary) and parse data contained in a single target file linked to by target field"""
        meta_files = self.metadata[[field_title]]
        if len(meta_files):
            filenames = set.union(*(
                set(split(r'\s*,\s*', e)) for e in meta_files.values.flatten()
            ))
            target_filenames = {
                fn for fn in filenames if not search(blacklist_regex, fn)
            }
            if len(target_filenames) == 0:
                raise GeneLabFileException(
                    "No suitable normalized annotated data files found"
                )
            elif len(target_filenames) > 1:
                raise GeneLabFileException(
                    "Multiple normalized annotated data files found"
                )
            else:
                filename = target_filenames.pop()
                url = self._get_file_url(filename)
                fetch_file(filename, url, self.storage, update=force_redownload)
                csv = join(self.storage, filename)
                data = read_csv(csv, sep=sep, index_col=0)
                data.columns.name = self._indexed_by
                if translate_sample_names:
                    data = self._translate_data_sample_names(data, data_columns)
                if self._name_delim != DELIM_AS_IS:
                    data.columns = data.columns.map(
                        lambda f: sub(r'[._-]', self._name_delim, f)
                    )
                return data
        else:
            return None

    def get_normalized_data(self, force_redownload=False, translate_sample_names=False, data_columns="sample name"):
        """Get normalized data from file(s) listed under 'normalized data files'"""
        self._normalized_data = self._read_data_from(
            ".*normalized data files.*",
            blacklist_regex=r'\.rda(ta)?(\.gz)?$',
            force_redownload=force_redownload,
            translate_sample_names=translate_sample_names,
            data_columns=data_columns
        )
        return self._normalized_data

    @property
    def normalized_data(self):
        if self._normalized_data is None:
            return self.get_normalized_data()
        else:
            return self._normalized_data

    def get_processed_data(self, force_redownload=False, translate_sample_names=False, data_columns="sample name"):
        """Get processed data from file(s) listed under 'normalized annotated data files'"""
        self._processed_data = self._read_data_from(
            ".*normalized annotated data files.*",
            blacklist_regex=r'\.rda(ta)?(\.gz)?$',
            force_redownload=force_redownload,
            translate_sample_names=translate_sample_names,
            data_columns=data_columns
        )
        return self._processed_data

    @property
    def processed_data(self):
        if self._processed_data is None:
            return self.get_processed_data()
        else:
            return self._processed_data

    @property
    def gct(self):
        pdata = self.processed_data.copy()
        pdata.insert(loc=0, column="Description", value=pdata.index)
        pdata.insert(loc=0, column="Name", value=pdata.index)
        gct_header = "#1.2\n{}\t{}\n".format(pdata.shape[0], pdata.shape[1]-2)
        return gct_header + pdata.to_csv(sep="\t", index=False)

    # alias:
    def get_normalized_annotated_data(self, force_redownload=False):
        return self.get_processed_data(force_redownload=force_redownload)

    # alias:
    @property
    def normalized_annotated_data(self):
        return self.processed_data


class AssayDispatcher(dict):
    """Contains Assay objects, indexable by name or by attributes"""

    def __init__(self, parent, json, glds_file_urls, storage_prefix, index_by, name_delim):
        """Populate dictionary of assay_name -> Assay()"""
        try:
            for assay_name, assay_json in json.items():
                super().__setitem__(
                    assay_name,
                    Assay(
                        parent, assay_name, assay_json, index_by=index_by,
                        name_delim=name_delim, storage_prefix=storage_prefix,
                        glds_file_urls=glds_file_urls
                    )
                )
        except KeyError:
            raise GeneLabJSONException(
                "Malformed assay JSON ({})".format(self.accession)
            )

    @property
    def _summary_dataframe(self):
        """List assay names and types"""
        repr_rows = []
        for assay_name, assay in self.items():
            protocol_set = set(map(
                str.lower,
                assay.metadata[["Protocol REF"]].values.flatten()
            ))
            factors = set(
                sub('^factor value:\s+', "", f, flags=IGNORECASE)
                for f in assay.factors().columns
            )
            for factor in factors:
                repr_row = [assay_name, factor]
                for characteristic in ASSAY_CHARACTERISTICS:
                    repr_row.append(
                        len(assay._match_field_titles(characteristic)) > 0
                    )
                for protocol in ASSAY_PROTOCOL_LIST:
                    repr_row.append(protocol in protocol_set)
                repr_rows.append(repr_row)
        repr_dataframe = DataFrame(
            data=repr_rows,
            columns=(
                ["name", "factors"] +
                ASSAY_CHARACTERISTICS +
                ASSAY_PROTOCOL_LIST
            )
        )
        return repr_dataframe.copy()

    def __repr__(self):
        return (
            "Dictionary of {} assays;\n".format(len(self.keys())) +
            "Subsettable with .choose() by following properties:\n" +
            repr(self._summary_dataframe.T)
        )

    def choose(self, factors=None, material_type=None, has_arrays=None, has_normalized_data=None, has_processed_data=None):
        """Subset AssayDispatcher by properties"""
        rd = self._summary_dataframe
        subsetter = (rd["has_arrays"]!=2) # always true
        if factors is not None:
            subsetter &= (rd["factors"]==factors)
        if has_arrays is not None:
            subsetter &= (rd["has_arrays"]==has_arrays)
        if has_normalized_data is not None:
            subsetter &= (rd["has_normalized_data"]==has_normalized_data)
        if has_processed_data is not None:
            subsetter &= (rd["has_processed_data"]==has_processed_data)
        chosen_names = set(rd[subsetter].index)
        chosen_subset = deepcopy(self)
        for name in self.keys():
            if name not in chosen_names:
                del chosen_subset[name]
        return chosen_subset
