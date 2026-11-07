#!/usr/bin/env python3
"""Bound one application's local image history without pruning shared Docker data."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess

KEEP = 5
PROJECTS = {
    'schooltest5': ('yrskrs/school_test', ('schooltest5-app', 'schooltest5-candidate', 'schooltest5-rollback', 'schooltest-rollback', 'school_test-app')),
    'schoolwork2': ('yrskrs/schoolwork2', ('schoolwork2-app', 'schoolwork2-candidate', 'schoolwork2-rollback')),
    'schooljournal': ('yrskrs/schooljournal', ('schooljournal-web', 'schooljournal-candidate', 'schooljournal-rollback')),
}
PROJECTS['school_test'] = PROJECTS['schooltest5']


def repository(tag):
    return tag.rsplit(':', 1)[0] if ':' in tag.rsplit('/', 1)[-1] else tag


def owned_tags(image, project):
    prefixes = PROJECTS[project][1]
    return [tag for tag in image.get('RepoTags') or [] if repository(tag) in prefixes]


def belongs(image, project):
    if owned_tags(image, project):
        return True
    # Untagged former app builds are identifiable by both OCI source and version.
    labels = image.get('Config', {}).get('Labels') or {}
    source = labels.get('org.opencontainers.image.source', '').removesuffix('.git').rstrip('/')
    expected = 'https://github.com/' + PROJECTS[project][0]
    return not image.get('RepoTags') and source == expected and bool(labels.get('org.opencontainers.image.version'))


def plan(images, project, protected=(), keep=KEEP):
    if keep < 1:
        raise ValueError('keep must be positive')
    unique = {image['Id']: image for image in images if belongs(image, project)}
    ordered = sorted(unique.values(), key=lambda image: (
        datetime.fromisoformat(image['Created'].replace('Z', '+00:00')).timestamp(), image['Id']), reverse=True)
    retained = set(protected) & unique.keys()
    for image in ordered:
        if len(retained) >= keep:
            break
        retained.add(image['Id'])
    return {
        'keep': sorted(retained),
        'remove': [image for image in reversed(ordered) if image['Id'] not in retained],
        'protected_overflow': len(retained) > keep,
        'total': len(unique),
    }


class Docker:
    def run(self, *args):
        result = subprocess.run(['docker', *args], check=True, capture_output=True, text=True, timeout=120)
        return result.stdout.strip()

    def images(self):
        ids = sorted(set(self.run('image', 'ls', '--all', '--no-trunc', '--quiet').splitlines()))
        images = []
        for offset in range(0, len(ids), 50):
            images.extend(json.loads(self.run('image', 'inspect', *ids[offset:offset + 50])))
        return images

    def referenced_images(self):
        containers = self.run('ps', '--all', '--quiet', '--no-trunc').splitlines()
        result = set()
        for offset in range(0, len(containers), 50):
            result.update(self.run('container', 'inspect', '--format', '{{.Image}}',
                                   *containers[offset:offset + 50]).splitlines())
        return result

    def resolve(self, reference):
        return self.run('image', 'inspect', '--format', '{{.Id}}', reference)


def rollback_references(root):
    """Preserve the immediate rollback recorded by the web updater, also from CLI."""
    path = Path(root) / 'backups/update_status.json'
    if not path.exists():
        return []
    state = json.loads(path.read_text())
    image = (state.get('rollback') or {}).get('image')
    return [image] if image else []


def cleanup(project, protect=(), keep=KEEP, apply=False, docker=None, log=print, rollback=()):
    docker = docker or Docker()
    # Complete the inventory and resolve every explicit safety reference before deleting anything.
    protected = docker.referenced_images()
    protected.update(docker.resolve(reference) for reference in protect)
    images = docker.images()
    references = {reference: image['Id'] for image in images
                  for reference in [image['Id'], *(image.get('RepoTags') or []), *(image.get('RepoDigests') or [])]}
    for reference in rollback:
        if reference in references:
            protected.add(references[reference])
        else:
            log('Образ старого запису відкату вже відсутній у Docker: ' + reference)
    result = plan(images, project, protected, keep)
    log(f'Образи {project}: {result["total"]}; залишаємо {len(result["keep"])}; старих до очищення {len(result["remove"])}.')
    if result['protected_overflow']:
        log('Понад ліміт: образи використовуються контейнерами або потрібні для відкату; їх збережено.')
    result.update(removed=[], skipped=[], errors=[])
    for image in result['remove']:
        image_id = image['Id']
        refs = owned_tags(image, project) or [image_id]
        log(('Видалення: ' if apply else 'План видалення: ') + ', '.join(refs))
        if not apply:
            continue
        try:
            # A different process may have started a container or moved a tag since the plan.
            if image_id in docker.referenced_images():
                result['skipped'].append(image_id)
                log('Образ уже використовується контейнером; пропускаємо ' + image_id)
                continue
            for reference in refs:
                if docker.resolve(reference) != image_id:
                    result['skipped'].append(reference)
                    log('Тег змінився після перевірки; пропускаємо ' + reference)
                    continue
                # No force, volume/builder/system prune, or deletion of other repository aliases.
                docker.run('image', 'rm', '--no-prune', reference)
                result['removed'].append(reference)
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            result['errors'].append(image_id)
            log('Не вдалося очистити образ; його збережено: ' + image_id + ' (' + type(error).__name__ + ')')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', choices=sorted(PROJECTS), required=True)
    parser.add_argument('--keep', type=int, default=KEEP)
    parser.add_argument('--protect', action='append', default=[])
    parser.add_argument('--apply', action='store_true', help='Without this flag, only show the plan')
    args = parser.parse_args(argv)
    if args.keep < 1:
        parser.error('--keep must be positive')
    rollback = rollback_references(Path(__file__).resolve().parents[1])
    result = cleanup(args.project, args.protect, args.keep, args.apply, rollback=rollback)
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
