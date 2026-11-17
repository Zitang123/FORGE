"""Exact determinant and inverse of a 2x2 matrix."""
import argparse
from fractions import Fraction
import json
import sys


def _matrix(value):
    if type(value) is not list or len(value)!=2:
        raise ValueError('Matrix must be a 2x2 list of lists')
    result=[]
    for row in value:
        if type(row) is not list or len(row)!=2:
            raise ValueError('Matrix must be a 2x2 list of lists')
        if any(type(x) not in (int,Fraction) for x in row):
            raise ValueError('Entries must be exact integers or Fraction values; booleans and floats are not accepted')
        result.append([Fraction(x) for x in row])
    return result


def determinant(matrix):
    """Return ad-bc as an exact Fraction; never mutate the input."""
    [[a,b],[c,d]]=_matrix(matrix)
    return a*d-b*c


def inverse(matrix):
    """Return an exact Fraction matrix, or raise ValueError if singular."""
    [[a,b],[c,d]]=_matrix(matrix)
    det=a*d-b*c
    if det==0:
        raise ValueError('Singular matrix: determinant is zero, so no inverse exists')
    return [[d/det,-b/det],[-c/det,a/det]]


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('entries',nargs=4,metavar='ENTRY',help='Row-major exact integer or rational a/b')
    args=parser.parse_args(argv)
    try:
        a,b,c,d=[Fraction(x) for x in args.entries]
        matrix=[[a,b],[c,d]]
        result={'determinant':str(determinant(matrix)),
                'inverse':[[str(x) for x in row] for row in inverse(matrix)]}
    except (ValueError,ZeroDivisionError,OverflowError) as error:
        print('matrix_lab: '+str(error),file=sys.stderr)
        return 2
    print(json.dumps(result,sort_keys=True,allow_nan=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
