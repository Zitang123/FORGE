import copy
from fractions import Fraction as F
import json
from pathlib import Path
import subprocess
import sys
import unittest

from matrix_lab import determinant,inverse


class ExactMatrixTests(unittest.TestCase):
    def test_integer_determinant_and_inverse(self):
        matrix=[[1,2],[3,4]]
        self.assertEqual(determinant(matrix),F(-2))
        self.assertEqual(inverse(matrix),[[F(-2),F(1)],[F(3,2),F(-1,2)]])

    def test_rational_inverse_identity_and_input_preservation(self):
        matrices=[[[F(1,3),F(2,7)],[F(4,5),F(3,2)]],[[0,2],[3,0]],[[1,0],[0,1]]]
        for matrix in matrices:
            before=copy.deepcopy(matrix);inv=inverse(matrix)
            product=[[sum((matrix[i][k]*inv[k][j] for k in range(2)),F(0)) for j in range(2)] for i in range(2)]
            self.assertEqual(product,[[F(1),F(0)],[F(0),F(1)]])
            self.assertEqual(matrix,before)
            self.assertTrue(all(type(x) is F for row in inv for x in row))

    def test_singular(self):
        for matrix in [[[1,2],[2,4]],[[0,0],[0,0]]]:
            self.assertEqual(determinant(matrix),0)
            with self.assertRaisesRegex(ValueError,'Singular'):inverse(matrix)

    def test_invalid_shapes_and_inexact_entries(self):
        invalid=[None,[],{},[[1,2]],[[1,2],[3]],((1,2),(3,4)),[[1,2],(3,4)],[[True,2],[3,4]],[[1.0,2],[3,4]],[[float('nan'),2],[3,4]],[[float('inf'),2],[3,4]],[['1',2],[3,4]],[[1+0j,2],[3,4]]]
        for value in invalid:
            for fn in [determinant,inverse]:
                with self.subTest(value=value,fn=fn.__name__),self.assertRaises(ValueError):fn(value)

    def cli(self,*args):
        return subprocess.run([sys.executable,str(Path(__file__).with_name('matrix_lab.py')),*args],capture_output=True,text=True,timeout=10)

    def test_real_cli_rational_json(self):
        result=self.cli('1/2','0','0','2/3')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{'determinant':'1/3','inverse':[['2','0'],['0','3/2']]})

    def test_real_cli_friendly_errors(self):
        for args in [('1','2','2','4'),('NaN','0','0','1'),('1/0','0','0','1')]:
            result=self.cli(*args)
            self.assertEqual(result.returncode,2)
            self.assertNotIn('Traceback',result.stderr)
            self.assertEqual(result.stdout,'')


if __name__=='__main__':unittest.main()
