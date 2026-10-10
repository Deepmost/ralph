"""极简账本模块 —— 对抗性 benchmark 的沙盒基线（仅标准库）。

这是一份**已通过既有测试**的基线代码，用于构造回归陷阱 / 受保护文件 / 多任务依赖链
等对抗场景。任务会在这个沙盒上做增量修改。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Account:
    name: str
    balance_cents: int = 0


class Ledger:
    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}

    def open_account(self, name: str) -> Account:
        if name in self._accounts:
            raise ValueError(f"account exists: {name}")
        self._accounts[name] = Account(name)
        return self._accounts[name]

    def accounts(self) -> list[Account]:
        return [self._accounts[n] for n in sorted(self._accounts)]

    def deposit(self, name: str, cents: int) -> int:
        if cents <= 0:
            raise ValueError("deposit must be positive")
        acct = self._accounts[name]
        acct.balance_cents += cents
        return acct.balance_cents

    def withdraw(self, name: str, cents: int) -> int:
        if cents <= 0:
            raise ValueError("withdraw must be positive")
        acct = self._accounts[name]
        if acct.balance_cents < cents:
            raise ValueError("insufficient funds")
        acct.balance_cents -= cents
        return acct.balance_cents

    def balance_cents(self, name: str) -> int:
        return self._accounts[name].balance_cents

    def total_cents(self) -> int:
        return sum(a.balance_cents for a in self._accounts.values())

    def format_amount(self, cents: int) -> str:
        sign = "-" if cents < 0 else ""
        return f"{sign}${abs(cents) / 100:.2f}"
