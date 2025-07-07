import unittest
import sqlcc


class SqlCcTest(unittest.TestCase):
    def test_simple(self):
        sql = "select * from table"
        result = sqlcc.calc(sql)
        self.assertEqual(1, result)

    def test_logic(self):
        sql = """
            select * 
            from table 
            where id = 2 
            and id = 3
            or id = 4
            and id in (1,2,3)
            and id between 3 and 4
            or id like '%'
            or exists (select * from b)
            group by id
        """
        result = sqlcc.calc(sql)
        self.assertEqual(18, result)

    def test_join(self):
        sql = """
            select * 
            from a
            join b on a.id = b.id 
        """
        result = sqlcc.calc(sql)
        self.assertEqual(3, result)


if __name__ == '__main__':
    unittest.main()
