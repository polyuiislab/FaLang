# F_IDE_Project/core/compiler/parser.py
# from.lexer import TokenType
# from.ast_nodes import *
# from.errors import ParserError
from typing import List

from .lexer import TokenType
from .ast_nodes import *
from .errors import ParserError

class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def parse(self) -> Program:
        #declarations =
        #tasks =
        declarations = []
        tasks = []
        
        while not self.is_at_end():
            token_type = self.current().type
            if token_type in (TokenType.RESOURCE, TokenType.BUFFER, TokenType.STORAGE, TokenType.MATERIAL):
                declarations.append(self.parse_declaration())
            elif token_type == TokenType.TASK:
                tasks.append(self.parse_task())
            else:
                raise ParserError(f"Unexpected token {self.current().value}", self.current().line, self.current().column)
                
        return Program(declarations, tasks)

    def parse_declaration(self) -> ASTNode:
        token = self.advance()
        if token.type == TokenType.MATERIAL:
            name = self.consume(TokenType.IDENTIFIER).value
            quantity = 1
            if self.match(TokenType.LBRACKET):
                quantity = self.consume(TokenType.NUMBER).value
                self.consume(TokenType.RBRACKET)
            self.consume(TokenType.SEMICOLON)
            return MaterialDecl(name, quantity)
        else:
            name = self.consume(TokenType.IDENTIFIER).value
            self.consume(TokenType.COLON)
            
            category = self.advance().value # Device, Register, Memory
            #capabilities =
            capabilities = []
            if self.match(TokenType.LANGLE):
                while not self.check(TokenType.RANGLE):
                    capabilities.append(self.advance().value)
                    if self.check(TokenType.COMMA): self.advance()
                self.consume(TokenType.RANGLE)
                
            capacity = None
            if token.type == TokenType.BUFFER and self.match(TokenType.LANGLE):
                self.consume(TokenType.CAPACITY)
                self.consume(TokenType.ASSIGN)
                capacity = self.consume(TokenType.NUMBER).value
                self.consume(TokenType.RANGLE)
                
            self.consume(TokenType.SEMICOLON)
            decl_kind = {
                TokenType.RESOURCE: "Resource",
                TokenType.BUFFER: "Buffer",
                TokenType.STORAGE: "Storage",
            }.get(token.type, "Resource")
            return ResourceDecl(name, category, capabilities, capacity, decl_kind)

    def parse_task(self) -> TaskDef:
        self.consume(TokenType.TASK)
        name = self.consume(TokenType.IDENTIFIER).value
        self.consume(TokenType.LPAREN)
        #params =
        params = []
        while not self.check(TokenType.RPAREN):
            if self.match(TokenType.MATERIAL):
                params.append(self.consume(TokenType.IDENTIFIER).value)
            elif self.match(TokenType.INT):
                params.append(self.consume(TokenType.IDENTIFIER).value)
            elif self.current().type == TokenType.IDENTIFIER:
                params.append(self.advance().value)
            if self.check(TokenType.COMMA): self.advance()
        self.consume(TokenType.RPAREN)
        
        self.consume(TokenType.LBRACE)
        body = self.parse_block()
        self.consume(TokenType.RBRACE)
        return TaskDef(name, params, body)

    def parse_block(self) -> List:
        #stmts =
        stmts = []
        while not self.check(TokenType.RBRACE) and not self.check(TokenType.CATCH) and not self.check(TokenType.COMPENSATE):
            stmts.append(self.parse_statement())
        return stmts

    def parse_statement(self) -> Statement:
        if self.match(TokenType.RETURN):
            ret_token = self.tokens[self.pos - 1]
            value = self.consume(TokenType.IDENTIFIER).value
            semi_token = self.consume(TokenType.SEMICOLON)
            return ReturnStmt(value, start_line=ret_token.line, end_line=semi_token.line)

        if self.match(TokenType.TRY):
            try_token = self.tokens[self.pos-1]
            self.consume(TokenType.LBRACE)
            try_block = self.parse_block()
            self.consume(TokenType.RBRACE)
            
            self.consume(TokenType.CATCH)
            self.consume(TokenType.LPAREN)
            exc_type = self.advance().value
            self.consume(TokenType.RPAREN)
            self.consume(TokenType.LBRACE)
            catch_block = self.parse_block()
            self.consume(TokenType.RBRACE)
            
            comp_block = []
            if self.match(TokenType.COMPENSATE):
                self.consume(TokenType.LBRACE)
                comp_block = self.parse_block()
                self.consume(TokenType.RBRACE)
            end_line = self.tokens[self.pos-1].line
            return TryCatchStmt(try_block, exc_type, catch_block, comp_block, start_line=try_token.line, end_line=end_line)
            
        elif self.match(TokenType.IF):
            if_token = self.tokens[self.pos-1]
            self.consume(TokenType.LPAREN)
            cond_var = self.consume(TokenType.IDENTIFIER).value
            self.consume(TokenType.EQ)
            cond_val = self.consume(TokenType.NUMBER).value
            self.consume(TokenType.RPAREN)
            self.consume(TokenType.LBRACE)
            true_block = self.parse_block()
            self.consume(TokenType.RBRACE)
            
            false_block = []
            if self.match(TokenType.ELSE):
                self.consume(TokenType.LBRACE)
                false_block = self.parse_block()
                self.consume(TokenType.RBRACE)
            end_line = self.tokens[self.pos-1].line
            return IfStmt(cond_var, cond_val, true_block, false_block, start_line=if_token.line, end_line=end_line)
            
        elif self.match(TokenType.PARALLEL):
            parallel_token = self.tokens[self.pos-1]
            self.consume(TokenType.LBRACE)
            block = self.parse_block()
            self.consume(TokenType.RBRACE)
            return ParallelBlock(block, start_line=parallel_token.line, end_line=self.tokens[self.pos-1].line)
            
        elif self.match(TokenType.FOREACH):
            foreach_token = self.tokens[self.pos-1]
            self.consume(TokenType.LPAREN)
            item_var = self.consume(TokenType.IDENTIFIER).value
            self.consume(TokenType.IN)
            collection = self.consume(TokenType.IDENTIFIER).value
            self.consume(TokenType.RPAREN)
            self.consume(TokenType.LBRACE)
            body = self.parse_block()
            self.consume(TokenType.RBRACE)
            return ForeachStmt(item_var, collection, body, start_line=foreach_token.line, end_line=self.tokens[self.pos-1].line)
            
        else:
            return self.parse_simple_stmt()

    def parse_simple_stmt(self) -> Statement:
        target = None
        if self.peek().type == TokenType.ASSIGN:
            target = self.consume(TokenType.IDENTIFIER).value
            self.consume(TokenType.ASSIGN)
            
        if self.match(TokenType.INT):
            int_token = self.tokens[self.pos - 1]
            target = self.consume(TokenType.IDENTIFIER).value
            self.consume(TokenType.ASSIGN)
            if self.current().type == TokenType.NUMBER and self.peek().type == TokenType.SEMICOLON:
                value = self.consume(TokenType.NUMBER).value
                semi_token = self.consume(TokenType.SEMICOLON)
                return IntDecl(target, value, start_line=int_token.line, end_line=semi_token.line)

        # Task call: IDENTIFIER '(' ... ')'
        if self.current().type == TokenType.IDENTIFIER and self.peek().type == TokenType.LPAREN:
            callee_token = self.advance()
            callee = callee_token.value
            start_line = callee_token.line
            self.consume(TokenType.LPAREN)
            args = []
            while not self.check(TokenType.RPAREN):
                if self.current().type == TokenType.STRING:
                    args.append(self.advance().value)
                elif self.current().type == TokenType.NUMBER:
                    args.append(str(self.advance().value))
                elif self.current().type == TokenType.IDENTIFIER:
                    args.append(self.advance().value)
                if self.check(TokenType.COMMA):
                    self.advance()
            self.consume(TokenType.RPAREN)
            semi_token = self.consume(TokenType.SEMICOLON)
            return CallStmt(callee, args, target, start_line=start_line, end_line=semi_token.line)

        # Built-in operation
        op_token = self.advance()
        op_type = op_token.value
        start_line = op_token.line
        self.consume(TokenType.LPAREN)
        args = []
        param_str = None

        while not self.check(TokenType.RPAREN):
            if self.current().type == TokenType.STRING:
                param_str = self.advance().value
            elif self.current().type == TokenType.NUMBER:
                args.append(str(self.advance().value))
            elif self.current().type == TokenType.IDENTIFIER:
                args.append(self.advance().value)
            if self.check(TokenType.COMMA):
                self.advance()

        self.consume(TokenType.RPAREN)
        semi_token = self.consume(TokenType.SEMICOLON)
        end_line = semi_token.line
        return Operation(op_type, args, target, param_str, start_line=start_line, end_line=end_line)

    # Helper methods
    def current(self): return self.tokens[self.pos]
    def advance(self): self.pos += 1; return self.tokens[self.pos - 1]
    def peek(self): return self.tokens[self.pos + 1]
    def is_at_end(self): return self.current().type == TokenType.EOF
    def check(self, t): return not self.is_at_end() and self.current().type == t
    def match(self, t):
        if self.check(t): self.advance(); return True
        return False
    def consume(self, t):
        if self.check(t): return self.advance()
        raise ParserError(f"Expected {t}, got {self.current().type}", self.current().line, self.current().column)
