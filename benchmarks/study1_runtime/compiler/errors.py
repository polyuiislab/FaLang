class CompilerError(Exception):
    def __init__(self, message, line=None, column=None):
        self.message = message
        self.line = line
        self.column = column
        super().__init__(self.message)

    def __str__(self):
        if self.line is not None and self.column is not None:
            return f"[Line {self.line}:{self.column}] {self.message}"
        return self.message

class LexerError(CompilerError): pass
class ParserError(CompilerError): pass
class SemanticError(CompilerError): pass