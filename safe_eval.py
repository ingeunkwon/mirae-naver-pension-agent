#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
공식 계산기 — eval()을 쓰지 않는다.

DB에 든 문자열이라도 eval()에 그대로 넣으면 안 된다. DB는 언젠가
스크립트로 갱신되고, 그 스크립트에 들어가는 값은 언젠가 사람이 아닌 것이
쓴다. 그때 eval()은 그대로 원격 코드 실행이 된다.

여기서는 AST로 파싱한 뒤 허용된 노드만 직접 계산한다.
허용: 숫자, 변수명(주어진 것만), + - * / // % **, 단항 -, 괄호, min/max/round/abs
그 외(속성 접근, 인덱싱, 호출, 비교, 람다, 이름 없는 변수)는 전부 거부한다.
"""
from __future__ import annotations
import ast, operator

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"min": min, "max": max, "round": round, "abs": abs}

MAX_POW = 32          # 2**999999 같은 자원 고갈 방지


class UnsafeExpression(ValueError):
    pass


def _eval(node, vars_: dict[str, float]):
    if isinstance(node, ast.Expression):
        return _eval(node.body, vars_)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise UnsafeExpression(f"허용되지 않는 상수: {node.value!r}")
        return node.value

    if isinstance(node, ast.Name):
        if node.id not in vars_:
            raise UnsafeExpression(f"정의되지 않은 변수: {node.id}")
        return vars_[node.id]

    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise UnsafeExpression(f"허용되지 않는 연산자: {type(node.op).__name__}")
        left, right = _eval(node.left, vars_), _eval(node.right, vars_)
        if isinstance(node.op, ast.Pow) and (abs(right) > MAX_POW):
            raise UnsafeExpression("지수가 너무 큽니다")
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise ZeroDivisionError("0으로 나눔")
        return op(left, right)

    if isinstance(node, ast.UnaryOp):
        op = _UNARY.get(type(node.op))
        if op is None:
            raise UnsafeExpression(f"허용되지 않는 단항 연산자: {type(node.op).__name__}")
        return op(_eval(node.operand, vars_))

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise UnsafeExpression("허용되지 않는 함수 호출")
        if node.keywords:
            raise UnsafeExpression("키워드 인자는 허용하지 않습니다")
        return _FUNCS[node.func.id](*[_eval(a, vars_) for a in node.args])

    raise UnsafeExpression(f"허용되지 않는 문법: {type(node).__name__}")


def safe_eval(expression: str, variables: dict[str, float]) -> float:
    """산술식을 안전하게 계산한다. 허용 범위를 벗어나면 UnsafeExpression."""
    if len(expression) > 500:
        raise UnsafeExpression("수식이 너무 깁니다")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise UnsafeExpression(f"파싱 실패: {e}") from e
    return _eval(tree, dict(variables))


def check_expression(expression: str, var_names: list[str]) -> tuple[bool, str]:
    """DB 적재 전 정적 검증. 변수는 선언된 것만 쓰는지까지 본다."""
    try:
        safe_eval(expression, {n: 1.0 for n in var_names})
        return True, "ok"
    except ZeroDivisionError:
        return True, "ok (샘플값에서 0으로 나눔 — 수식 자체는 안전)"
    except UnsafeExpression as e:
        return False, str(e)


if __name__ == "__main__":
    ok = [("account_value / (11 - year_n) * 1.2", ["account_value", "year_n"]),
          ("2000000 + medical_cost + leave_months * 1500000",
           ["medical_cost", "leave_months"]),
          ("min(paid, 9000000) * 0.165", ["paid"])]
    bad = [("__import__('os').system('ls')", []),
           ("account_value.__class__", ["account_value"]),
           ("[x for x in range(10)]", []),
           ("open('/etc/passwd').read()", []),
           ("2 ** 999999", [])]
    for e, v in ok:
        print("OK  ", check_expression(e, v), "|", e)
    for e, v in bad:
        print("BLOCK", check_expression(e, v), "|", e)
