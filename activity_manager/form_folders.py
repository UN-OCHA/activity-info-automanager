"""Database-scoped folder names and form locations, separate from form schemas."""

def folder_paths(database, resources):
    folders = {key: item for key, item in resources.items() if item.get('type') == 'FOLDER'}
    paths = {database: '/'}
    def path(identity, visiting=()):
        if identity in paths:
            return paths[identity]
        if identity in visiting or identity not in folders:
            raise ValueError('The database contains an invalid folder hierarchy.')
        item = folders[identity]
        parent = path(item.get('parentId') or database, (*visiting, identity))
        paths[identity] = (parent.rstrip('/') + '/' + str(item.get('label') or identity)).lstrip('/')
        return paths[identity]
    for identity in folders:
        path(identity)
    return paths


def resolve_folder(database, resources, value):
    paths = folder_paths(database, resources)
    if value == '/':
        return database, '/'
    if value in paths:
        return value, paths[value]
    if value.startswith('/'):
        matches = [identity for identity, path in paths.items() if path == value[1:]]
        if len(matches) == 1:
            return matches[0], paths[matches[0]]
        raise ValueError(f'Folder path {value!r} must identify exactly one folder; use an ID instead.')
    matches = [identity for identity, path in paths.items() if identity != database and
               (path == value or resources[identity].get('label') == value)]
    if len(matches) != 1:
        raise ValueError(f'Folder {value!r} must resolve to exactly one folder in the target database; use a folder ID or path.')
    return matches[0], paths[matches[0]]


def folder_choices(database, resources):
    from collections import Counter
    paths = folder_paths(database, resources)
    counts = Counter(paths.values())
    return sorted((identity if counts[path] > 1 else '/' + path.lstrip('/')
                   for identity, path in paths.items()), key=str.casefold)


def form_location(database, resources, identity):
    resource = resources.get(identity)
    if not resource or resource.get('type') != 'FORM':
        raise ValueError('Only a top-level form can be moved to a folder; subforms remain attached to their parent form.')
    parent = resource.get('parentId') or database
    paths = folder_paths(database, resources)
    if parent not in paths:
        raise ValueError('The form has an invalid parent folder.')
    return dict(databaseId=database, parentId=parent, path=paths[parent])
