"""Exercise retained family evidence through the production model-size boundary."""


def unpack(rows):
    return [dict(zip(rows['columns'], row)) for row in rows['rows']] if isinstance(rows, dict) else rows
