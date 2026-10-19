from fastmcp import FastMCP

mcp = FastMCP("demo")

@mcp.tool
def greet(name: str) -> str:
    """Say hello to someone."""
    return f"Hello, {name}!"

@mcp.tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=9001)
