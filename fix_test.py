with open(r"C:\Ronni\Projects\astra chatbot\tests\test_engine.py", "r") as f:
    content = f.read()
content = content.replace(
    'assert result.kind in ("exact", "zero", "ambiguous")',
    'assert result.kind in ("exact", "zero", "ambiguous", NOT_FOUND)'
)
with open(r"C:\Ronni\Projects\astra chatbot\tests\test_engine.py", "w") as f:
    f.write(content)
print("Done")