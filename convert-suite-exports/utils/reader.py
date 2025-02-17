import semver
from PIL import Image, ImageDraw
import numpy as np
from decimal import Decimal, ROUND_DOWN


def read_project(project_json):
    project_type = project_json.get('type', 'image-default')
    if project_type == 'image-siesta':
        return read_siesta_project(project_json)
    elif project_type == 'image-default':
        return read_death_valley_project(project_json)
    raise NotImplementedError


def read_siesta_project(project_json):
    if semver.compare(project_json['version'], '0.4.0') < 0:
        project_type = 'siesta-v1'
    else:
        project_type = 'siesta-v2'
    object_class_to_group_name = {
        o_id: g['name']
        for g in project_json['object_detection']['object_groups']
            for o_id in g['object_class_ids']
    }
    categories = []
    for o in project_json['object_detection']['object_classes']:
        if o['annotation_type'] in ['box', 'polygon']:
            categories.append({
                'id': len(categories) + 1,
                'name': o['name'],
                'supercategory': object_class_to_group_name.get(o['id'])
            })
    return project_type, categories


def read_death_valley_project(project_json):
    object_class_to_group_name = {
        o_name: g['name']
        for g in project_json['groups']
            for o_name in g['info']['classes']
    }
    categories = []
    for o in project_json['objects']:
        shapes = list(o['info']['shapes'].keys())
        if shapes[0] in ['box', 'polygon']:
            categories.append({
                'id': o['class_id'],
                'name': o['class_name'],
                'supercategory': object_class_to_group_name.get(o['class_name'])
            })
    return 'death-valley', categories


def read_meta(meta_map):
    images = []
    labels = {}
    for idx, ((dataset, data_key), meta) in enumerate(meta_map.items()):
        if 'height' not in meta['image_info'] or 'width' not in meta['image_info']:
            raise Exception('Only labels annotated through annotation app is supported.')
        images.append({
            'id': idx,
            'license': None,
            'dataset': dataset,
            'file_name': data_key,
            'height': meta['image_info']['height'],
            'width': meta['image_info']['width'],
            'date_captured': None,
        })
        labels[meta['label_id']] = {
            'image_id': idx
        }
    return images, labels


def read_labels(labels, project_type, categories, images):
    annotations = []
    image_map = {i['id']: i for i in images}
    category_map = {c['name']: c['id'] for c in categories}
    for label_id, label_info in labels.items():
        image_id, label = label_info['image_id'], label_info['label']
        if project_type == 'death-valley':
            annotations_in_label = read_death_valley_label(label, category_map, image_map[image_id])
        elif project_type in ['siesta-v1', 'siesta-v2']:
            annotations_in_label = read_siesta_label(label, project_type, category_map, image_map[image_id])
        else:
            raise NotImplementedError
        for anno in annotations_in_label:
            if anno.get('segmentation') is not None and 'counts' not in anno['segmentation']:
                anno['segmentation'] = [[
                    Decimal(x).quantize(Decimal('.01'), rounding=ROUND_DOWN)
                    for x in anno['segmentation'][0]
                ]]
            anno['bbox'] = [Decimal(x).quantize(Decimal('.01'), rounding=ROUND_DOWN) for x in anno['bbox']]
            anno['area'] = Decimal(anno['area']).quantize(Decimal('.01'), rounding=ROUND_DOWN) 

            annotations.append({
                'id': len(annotations) + 1,
                'image_id': image_id,
                'iscrowd': 0,
                **anno
            })
    return annotations


def read_death_valley_label(label, category_map, image):
    annotations = []
    for o in label['result']['objects']:
        if 'bbox' in o['shape']:
            bbox = [o['shape']['x'], o['shape']['y'], o['shape']['width'], o['shape']['height']]
            area = o['shape']['width'] * o['shape']['height']
            segmentation = None
        elif 'polygon' in o['shape']:
            bbox, area, segmentation, _ = convert_polygon_to_coco(o['shape']['polygon'], image)
            # bbox, area, _, segmentation = convert_polygon_to_coco(o['annotation']['coord']['points'], image) # Use this for RLE segmentation
        else:
            continue
        annotations.append({
            'category_id': category_map[o['class']],
            'bbox': bbox,
            'area': area,
            'segmentation': segmentation,
        })
    return annotations


def read_siesta_label(label, project_type, category_map, image):
    if project_type == 'siesta-v1':
        ANNO_KEY = 'annotationType'
        CLASS_NAME_KEY = 'className'
    else:
        ANNO_KEY = 'annotation_type'
        CLASS_NAME_KEY = 'class_name'
    annotations = []
    for o in label['objects']:
        if o[ANNO_KEY] == 'box':
            c = o['annotation']['coord']
            bbox = [c['x'], c['y'], c['width'], c['height']]
            area = c['width'] * c['height']
            segmentation = None
        elif o[ANNO_KEY] == 'polygon':
            if o['annotation'].get('multiple', False):
                # Polygon point segmentation is not available in multipolygon
                bbox, area, _, segmentation = convert_multi_polygon_to_coco(o['annotation']['coord']['points'], image)
            else:
                bbox, area, segmentation, _ = convert_polygon_to_coco(o['annotation']['coord']['points'], image)
                # bbox, area, _, segmentation = convert_polygon_to_coco(o['annotation']['coord']['points'], image) # Use this for RLE segmentation
        else:
            continue

        annotations.append({
            'category_id': category_map[o[CLASS_NAME_KEY]],
            'bbox': bbox,
            'area': area,
            'segmentation': segmentation,
        })
    return annotations


def to_coco_polygon(suite_poly):
    return [z for v in suite_poly for z in [v['x'], v['y']]]


def convert_polygon_to_coco(points, image):
    polygon = to_coco_polygon(points)
    mask_image = Image.new('L', (image['width'], image['height']), 0)
    ImageDraw.Draw(mask_image).polygon(polygon, outline=1, fill=1)
    mask = np.array(mask_image)

    from pycocotools import _mask as coco_mask
    mask = np.asfortranarray(mask.reshape(mask.shape[0], mask.shape[1], 1))
    rle = coco_mask.encode(mask)
    bbox = list(coco_mask.toBbox(rle)[0])
    area = int(coco_mask.area(rle)[0])

    return bbox, area, [polygon], rle[0]


def convert_multi_polygon_to_coco(points, image):
    """
    points is triple nested list of points consist of x and y.
    * Ref: Multipolygon in https://en.wikipedia.org/wiki/GeoJSON#Geometries
    """
    mask_image = Image.new('L', (image['width'], image['height']), 0)
    for face_polygons in points:
        ImageDraw.Draw(mask_image).polygon(to_coco_polygon(face_polygons[0]), outline=1, fill=1)
        for hole in face_polygons[1:]:
            ImageDraw.Draw(mask_image).polygon(to_coco_polygon(hole), outline=1, fill=0)
    mask = np.array(mask_image)

    from pycocotools import _mask as coco_mask
    mask = np.asfortranarray(mask.reshape(mask.shape[0], mask.shape[1], 1))
    rle = coco_mask.encode(mask)
    bbox = list(coco_mask.toBbox(rle)[0])
    area = int(coco_mask.area(rle)[0])

    return bbox, area, None, rle[0]
