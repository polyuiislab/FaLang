import re
from enum import Enum, auto
from dataclasses import dataclass
from .errors import LexerError

class TokenType(Enum):

    RESOURCE = auto(); BUFFER = auto(); STORAGE = auto()
    DEVICE = auto(); REGISTER = auto(); MEMORY = auto()
    MATERIAL = auto(); CAPACITY = auto(); INT = auto()


    RESOURCEFAULT = auto(); MATERIALMISSING = auto(); TIMEOUT = auto()


    TASK = auto(); PARALLEL = auto()

    IF = auto(); ELSE = auto(); FOREACH = auto(); IN = auto()
    TRY = auto(); CATCH = auto(); COMPENSATE = auto()
    RETURN = auto()


    LOAD = auto(); STORE = auto(); PROCESS = auto()
    MOVE = auto(); INSPECT = auto(); ADJUST = auto(); ISAVAILABLE = auto()


    LBRACE = auto(); RBRACE = auto()   
    LPAREN = auto(); RPAREN = auto()   
    LBRACKET = auto(); RBRACKET = auto() 
    LANGLE = auto(); RANGLE = auto()   
    COMMA = auto(); COLON = auto(); SEMICOLON = auto()
    ASSIGN = auto() 
    EQ = auto()     


    IDENTIFIER = auto()
    NUMBER = auto()
    STRING = auto()
    EOF = auto()

@dataclass
class Token:
    type: TokenType
    value: str
    line: int
    column: int

class Lexer:
    def __init__(self, source_code):
        self.source = source_code
        self.pos = 0
        self.line = 1
        self.column = 1

        self.tokens = []

        self.keywords = {
            'Resource': TokenType.RESOURCE, 'Buffer': TokenType.BUFFER, 'Storage': TokenType.STORAGE,
            'Device': TokenType.DEVICE, 'Register': TokenType.REGISTER, 'Memory': TokenType.MEMORY,
            'Material': TokenType.MATERIAL, 'Capacity': TokenType.CAPACITY, 'int': TokenType.INT,
            'Task': TokenType.TASK, 'Parallel': TokenType.PARALLEL,

            'if': TokenType.IF, 'else': TokenType.ELSE, 'foreach': TokenType.FOREACH, 'in': TokenType.IN,
            'try': TokenType.TRY, 'catch': TokenType.CATCH, 'compensate': TokenType.COMPENSATE,
            'return': TokenType.RETURN,
            'Load': TokenType.LOAD, 'Store': TokenType.STORE, 'Process': TokenType.PROCESS,
            'Move': TokenType.MOVE, 'Inspect': TokenType.INSPECT, 'Adjust': TokenType.ADJUST,
            'IsAvailable': TokenType.ISAVAILABLE,
            'ResourceFault': TokenType.RESOURCEFAULT, 'MaterialMissing': TokenType.MATERIALMISSING, 
            'Timeout': TokenType.TIMEOUT
        }

    def tokenize(self):
        while self.pos < len(self.source):
            char = self.source[self.pos]

            if char.isspace():
                if char == '\n':
                    self.line += 1
                    self.column = 1
                else:
                    self.column += 1
                self.pos += 1
                continue

            if char == '/' and self.peek() == '/':
                while self.pos < len(self.source) and self.source[self.pos]!= '\n':
                    self.pos += 1
                continue

            if char == '=':
                if self.peek() == '=':
                    self._add_token(TokenType.EQ, '==')
                    self.pos += 1; self.column += 1
                else:
                    self._add_token(TokenType.ASSIGN, '=')
            elif char == '{': self._add_token(TokenType.LBRACE, '{')
            elif char == '}': self._add_token(TokenType.RBRACE, '}')
            elif char == '(': self._add_token(TokenType.LPAREN, '(')
            elif char == ')': self._add_token(TokenType.RPAREN, ')')
            elif char == '[': self._add_token(TokenType.LBRACKET, '[')
            elif char == ']': self._add_token(TokenType.RBRACKET, ']')
            elif char == '<': self._add_token(TokenType.LANGLE, '<')
            elif char == '>': self._add_token(TokenType.RANGLE, '>')
            elif char == ',': self._add_token(TokenType.COMMA, ',')
            elif char == ':': self._add_token(TokenType.COLON, ':')
            elif char == ';': self._add_token(TokenType.SEMICOLON, ';')

            elif char.isalpha() or char == '_':
                self._read_identifier()
            elif char.isdigit():
                self._read_number()
            elif char == '"':
                self._read_string()
            else:
                raise LexerError(f"Illegal character '{char}'", self.line, self.column)

        self.tokens.append(Token(TokenType.EOF, "EOF", self.line, self.column))
        return self.tokens

    def peek(self):
        return self.source[self.pos + 1] if self.pos + 1 < len(self.source) else ''

    def _add_token(self, token_type, value):
        self.tokens.append(Token(token_type, value, self.line, self.column))
        self.pos += 1
        self.column += 1

    def _read_identifier(self):
        start_col = self.column
        value = ''
        while self.pos < len(self.source) and (self.source[self.pos].isalnum() or self.source[self.pos] == '_'):
            value += self.source[self.pos]
            self.pos += 1
            self.column += 1
        token_type = self.keywords.get(value, TokenType.IDENTIFIER)
        self.tokens.append(Token(token_type, value, self.line, start_col))

    def _read_number(self):
        start_col = self.column
        value = ''
        while self.pos < len(self.source) and self.source[self.pos].isdigit():
            value += self.source[self.pos]
            self.pos += 1
            self.column += 1
        self.tokens.append(Token(TokenType.NUMBER, int(value), self.line, start_col))

    def _read_string(self):
        start_col = self.column
        self.pos += 1 
        self.column += 1
        value = ''
        while self.pos < len(self.source) and self.source[self.pos]!= '"':
            value += self.source[self.pos]
            self.pos += 1
            self.column += 1
        if self.pos >= len(self.source):
            raise LexerError("Unclosed string literal", self.line, start_col)
        self.pos += 1 
        self.column += 1
        self.tokens.append(Token(TokenType.STRING, value, self.line, start_col))
