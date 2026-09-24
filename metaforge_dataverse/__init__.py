"""Excel -> Dataverse importer package.

Public entry points:
    excel.parse_workbook(source)        parse an .xlsx (path | bytes | file-like)
    mapping.build_payload(parsed, ...)  parsed workbook + live schema -> dataset JSON
    client.DataverseClient              get_blocks / validate / create_dataset
    app.app                             FastAPI application (POST /dataverse/import)
"""

__all__ = ["excel", "mapping", "client"]
