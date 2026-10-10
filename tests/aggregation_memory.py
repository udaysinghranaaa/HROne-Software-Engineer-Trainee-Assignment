"""Small strict aggregation interpreter for isolated tests, not a MongoDB substitute.

Unknown operators fail explicitly. Production pipelines execute only in MongoDB.
Server syntax, planner behavior and Decimal128 edge cases require the manual live run.
"""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR


def path_value(value, path):
    if not path:
        return value
    first, *rest = path.split(".")
    if isinstance(value, list):
        return [path_value(item, path) for item in value]
    return path_value(value.get(first), ".".join(rest)) if isinstance(value, dict) else None


def expression(spec, doc, variables):
    if isinstance(spec, str) and spec.startswith("$$"):
        name, _, path = spec[2:].partition(".")
        return path_value(variables.get(name), path)
    if isinstance(spec, str) and spec.startswith("$"):
        return path_value(doc, spec[1:])
    if isinstance(spec, list):
        return [expression(item, doc, variables) for item in spec]
    if not isinstance(spec, dict):
        return spec
    if not any(key.startswith("$") for key in spec):
        return {key: expression(value, doc, variables) for key, value in spec.items()}
    op, arg = next(iter(spec.items()))
    ev = lambda value: expression(value, doc, variables)
    if op == "$literal":
        return arg
    if op == "$cond":
        return ev(arg[1]) if ev(arg[0]) else ev(arg[2])
    if op == "$let":
        nested = {**variables, **{key: ev(value) for key, value in arg["vars"].items()}}
        return expression(arg["in"], doc, nested)
    if op in ("$map", "$filter"):
        values = []
        for item in ev(arg["input"]):
            nested = {**variables, arg.get("as", "this"): item}
            if op == "$map":
                values.append(expression(arg["in"], doc, nested))
            elif expression(arg["cond"], doc, nested):
                values.append(item)
        return values
    if op == "$dateFromString":
        return datetime.fromisoformat(ev(arg["dateString"])).replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
    if op == "$dateAdd":
        assert arg["unit"] == "day"
        return ev(arg["startDate"]) + timedelta(days=ev(arg["amount"]))
    if op == "$dateToString":
        return ev(arg["date"]).strftime(arg["format"])
    if op == "$dayOfWeek":
        value = ev(arg["date"]) if isinstance(arg, dict) else ev(arg)
        return (value.weekday() + 1) % 7 + 1
    if op == "$ifNull":
        value = ev(arg[0])
        return ev(arg[1]) if value is None else value
    if op == "$arrayElemAt":
        values, index = ev(arg)
        return values[index] if values and -len(values) <= index < len(values) else None
    if op in ("$and", "$or"):
        return all(ev(value) for value in arg) if op == "$and" else any(ev(value) for value in arg)
    if op == "$in":
        left, right = ev(arg)
        return left in right
    if op in ("$eq", "$ne", "$gt", "$gte", "$lt", "$lte"):
        left, right = ev(arg)
        if op == "$eq": return left == right
        if op == "$ne": return left != right
        if left is None or right is None: return False
        return {"$gt": lambda: left > right, "$gte": lambda: left >= right,
                "$lt": lambda: left < right, "$lte": lambda: left <= right}[op]()
    if op == "$toDecimal":
        value = ev(arg)
        return Decimal(str(value)) if value is not None else None
    if op == "$toDouble":
        value = ev(arg)
        return float(value) if value is not None else None
    if op == "$abs": return abs(ev(arg))
    if op == "$floor": return ev(arg).to_integral_value(rounding=ROUND_FLOOR) if isinstance(ev(arg), Decimal) else int(ev(arg) // 1)
    if op == "$size": return len(ev(arg))
    if op == "$range": return list(range(*ev(arg)))
    if op == "$add": return sum(ev(arg))
    if op == "$sum": return sum(value for value in ev(arg) if value is not None)
    if op == "$multiply":
        result = 1
        for value in ev(arg): result *= value
        return result
    if op == "$divide":
        left, right = ev(arg)
        return left / right
    raise AssertionError("Unsupported test expression: " + op)


def matches(doc, query, variables):
    for field, value in query.items():
        if field == "$expr":
            if not expression(value, doc, variables): return False
        elif field == "$and":
            if not all(matches(doc, part, variables) for part in value): return False
        elif isinstance(value, dict):
            if not all(expression({op: ["$" + field, operand]}, doc, variables) for op, operand in value.items()): return False
        elif path_value(doc, field) != value:
            return False
    return True


def pipeline(documents, stages, collections, variables=None):
    variables = variables or {}
    docs = copy.deepcopy(documents)
    for stage in stages:
        op, arg = next(iter(stage.items()))
        if op == "$match":
            docs = [doc for doc in docs if matches(doc, arg, variables)]
        elif op == "$limit": docs = docs[:arg]
        elif op == "$sort":
            for field, direction in reversed(list(arg.items())):
                docs.sort(key=lambda doc: path_value(doc, field), reverse=direction < 0)
        elif op in ("$set", "$addFields", "$project"):
            projected = []
            for doc in docs:
                new = copy.deepcopy(doc) if op != "$project" else {}
                for field, value in arg.items():
                    if op == "$project" and value == 0: continue
                    new[field] = path_value(doc, field) if op == "$project" and value == 1 else expression(value, doc, variables)
                projected.append(new)
            docs = projected
        elif op == "$unwind":
            field = arg[1:]
            expanded = []
            for doc in docs:
                for value in path_value(doc, field):
                    expanded.append({**doc, field: value})
            docs = expanded
        elif op == "$lookup":
            for doc in docs:
                nested = {**variables, **{key: expression(value, doc, variables) for key, value in arg.get("let", {}).items()}}
                doc[arg["as"]] = pipeline(collections[arg["from"]], arg["pipeline"], collections, nested)
        elif op == "$group":
            groups = {}
            for doc in docs:
                key = expression(arg["_id"], doc, variables)
                groups.setdefault(key, []).append(doc)
            grouped = []
            for key, rows in groups.items():
                out = {"_id": key}
                for field, accumulator in arg.items():
                    if field == "_id": continue
                    acc, value = next(iter(accumulator.items()))
                    values = [expression(value, doc, variables) for doc in rows]
                    if acc == "$sum": out[field] = sum(value for value in values if value is not None)
                    elif acc == "$first": out[field] = values[0]
                    elif acc == "$avg":
                        numbers = [value for value in values if value is not None]
                        out[field] = sum(numbers) / len(numbers) if numbers else None
                    else: raise AssertionError("Unsupported accumulator " + acc)
                grouped.append(out)
            docs = grouped
        elif op == "$count": docs = [{arg: len(docs)}] if docs else []
        elif op == "$setWindowFields":
            docs = pipeline(docs, [{"$sort": arg["sortBy"]}], collections, variables)
            original = copy.deepcopy(docs)
            for index, doc in enumerate(docs):
                for field, output in arg["output"].items():
                    if "$rank" in output:
                        sort_field = next(iter(arg["sortBy"]))
                        doc[field] = next(i + 1 for i, row in enumerate(original) if row[sort_field] == doc[sort_field])
                    elif "$avg" in output:
                        low, high = output["window"]["documents"]
                        values = [expression(output["$avg"], row, variables) for row in original[max(0, index + low):index + high + 1]]
                        values = [value for value in values if value is not None]
                        doc[field] = sum(values) / len(values) if values else None
                    else: raise AssertionError("Unsupported window output")
        else: raise AssertionError("Unsupported test stage: " + op)
    return docs
