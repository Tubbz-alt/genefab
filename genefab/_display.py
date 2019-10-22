from flask import Response
from json import JSONEncoder, dumps
from pandas import DataFrame, option_context, Series
from re import sub


class SetEnc(JSONEncoder):
    """Allow dumps to convert sets to serializable lists"""
    def default(self, entry):
        if isinstance(entry, set):
            return list(entry)
        else:
            return JSONEncoder.default(self, entry)


def to_dataframe(obj):
    """Convert simple structured objects (dicts, list, tuples) to a DataFrame representation"""
    if isinstance(obj, dict):
        return DataFrame(
            columns=["key", "value"],
            data=[[k, v] for k, vv in obj.items() for v in vv]
        )
    else:
        return DataFrame(columns=["value"], data=obj)


def display_object(obj, display_rargs, index="auto"):
    """Select appropriate converter and mimetype for fmt"""
    if isinstance(obj, (dict, tuple, list)):
        if display_rargs["fmt"] == "json":
            return Response(dumps(obj, cls=SetEnc), mimetype="text/json")
        else:
            obj, index = to_dataframe(obj), False
    if index == "auto":
        index = (display_rargs["fmt"] == "json")
    if isinstance(obj, DataFrame):
        if display_rargs["fmt"] == "list":
            if obj.shape == (1, 1):
                obj_repr = sub(r'\s*,\s*', "\n", str(obj.iloc[0, 0]))
                return Response(obj_repr, mimetype="text/plain")
            else:
                raise ValueError("multiple cells selected")
        if display_rargs["top"] is not None:
            if display_rargs["top"].isdigit() and int(display_rargs["top"]):
                obj = obj[:int(display_rargs["top"])]
            else:
                raise ValueError("`top` must be a positive integer")
        if display_rargs["header"]:
            obj = DataFrame(columns=obj.columns, index=["header"])
        if display_rargs["fmt"] == "tsv":
            obj_repr = obj.to_csv(sep="\t", index=index, na_rep="NA")
            return Response(obj_repr, mimetype="text/plain")
        elif display_rargs["fmt"] == "html":
            with option_context("display.max_colwidth", -1):
                return obj.to_html(index=index, na_rep="NA", justify="left")
        elif display_rargs["fmt"] == "json":
            try:
                obj_repr = obj.to_json(index=index, orient="index")
                return Response(obj_repr, mimetype="text/json")
            except ValueError:
                obj_repr = obj.to_json(index=index, orient="records")
                return Response(obj_repr, mimetype="text/json")
        else:
            raise ValueError("wrong extension or type?")
    elif display_rargs["fmt"] == "raw":
        return Response(obj, mimetype="application")
    else:
        return NotImplementedError(
            "{} cannot be displayed".format(type(obj).__name__)
        )


def to_cls(dataframe, target, continuous="infer", space_sub=lambda s: sub(r'\s', "", s)):
    """Convert a presumed annotation/factor dataframe to CLS format"""
    sample_count = dataframe.shape[0]
    if continuous == "infer":
        try:
            _ = dataframe[target].astype(float)
            continuous = True
        except ValueError:
            continuous = False
    elif not isinstance(continuous, bool):
        if continuous == "0":
            continuous = False
        elif continuous == "1":
            continuous = True
        else:
            error_message = "`continuous` can be either boolean-like or 'infer'"
            raise TypeError(error_message)
    if continuous:
        cls_data = [
            ["#numeric"], ["#" + target],
            dataframe[target].astype(float)
        ]
    else:
        if space_sub is None:
            space_sub = lambda s: s
        classes = dataframe[target].unique()
        class2id = Series(index=classes, data=range(len(classes)))
        cls_data = [
            [sample_count, len(classes), 1],
            ["# "+space_sub(classes[0])] + [space_sub(c) for c in classes[1:]],
            [class2id[v] for v in dataframe[target]]
        ]
    return "\n".join([
        "\t".join([str(f) for f in fields]) for fields in cls_data
    ])