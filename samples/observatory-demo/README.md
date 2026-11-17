# Exact Matrix Lab

A small Python standard-library example for FORGE Observatory. It computes the determinant and inverse of a 2x2 matrix using exact rational arithmetic.

```sh
python3 matrix_lab.py 1 2 3 4
python3 matrix_lab.py 1/2 0 0 2/3
python3 -m unittest discover -v
```

The second command returns:

```json
{"determinant":"1/3","inverse":[["2","0"],["0","3/2"]]}
```

Library use:

```python
from fractions import Fraction
from matrix_lab import determinant, inverse

matrix = [[Fraction(1, 2), 0], [0, Fraction(2, 3)]]
assert determinant(matrix) == Fraction(1, 3)
assert inverse(matrix) == [[Fraction(2), Fraction(0)], [Fraction(0), Fraction(3, 2)]]
```

The API requires exactly two list rows of two entries. Entries must be exact `int` or `Fraction` values; booleans, floats, nonfinite values and strings are rejected. The input is never mutated. A singular matrix has determinant zero, and `inverse()` raises `ValueError`. The CLI returns a friendly stderr error and exit code 2 for singular or invalid input.

The design separates validation, pure exact arithmetic and the JSON CLI. No visual interface, network, installation or external integration is needed. Observatory events record actual work; they do not measure hidden model activity or token usage.
