# Copyright 2014 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import os
import re
import json
import sys

import webapp2
from google.appengine.ext.webapp import template
from google.appengine.api import images
from google.appengine.api import urlfetch

sys.path.append(os.path.join(os.path.dirname(__file__),'../lib'))
from bs4 import BeautifulSoup
import cloudstorage as gcs

from handlers.common import *
from models import FeaturedArtwork


THUMB_HEIGHT=600
NO_CROP_TUPLE=(0, 0, 1, 1)


def artwork_dict(a):
  d = dict(
      id=a.key().id(),
      title=a.title,
      byline=a.byline,
      imageUri=a.image_url,
      thumbUri=a.thumb_url,
      detailsUri=a.details_url,
      publishDate=date_to_timestamp(a.publish_date),)
  if a.attribution:
      d['attribution'] = a.attribution
  return d


class BaseBackroomHandler(webapp2.RequestHandler):
  def handle_exception(self, exception, debug):
    # Log the error.
    logging.exception(exception)

    # Set a custom message.
    self.response.write(exception.message)

    # If the exception is a HTTPException, use its error code.
    # Otherwise use a generic 500 error code.
    if isinstance(exception, webapp2.HTTPException):
      self.response.set_status(exception.code)
    else:
      self.response.set_status(500)


class ServiceListHandler(BaseBackroomHandler):
  def get(self):
    self.response.headers['Content-Type'] = 'application/json'
    self.response.out.write(self.render())

  def render(self):
    start = datetime.date(day=1,
        month=int(self.request.get('month')) + 1,
        year=int(self.request.get('year')))
    start -= datetime.timedelta(weeks=2)
    queue = (FeaturedArtwork.all()
        .filter('publish_date >=', start)
        .order('publish_date')
        .fetch(1000))
    return json.dumps([artwork_dict(a) for a in queue])


def maybe_process_image(image_url, crop_tuple, base_name):
  if CLOUD_STORAGE_ROOT_URL in image_url and crop_tuple == NO_CROP_TUPLE:
    return (image_url, None)

  image_result = urlfetch.fetch(image_url, deadline=20)
  if image_result.status_code < 200 or image_result.status_code >= 300:
    raise IOError('Error downloading image: HTTP %d.' % image_result.status_code)

  filename = re.sub(r'[^\w]+', '-', base_name.strip().lower()) + '.jpg'

  # main image
  image_gcs_path = CLOUD_STORAGE_BASE_PATH + '/fullres/' + filename
  # resize to max width 4000 or max height 2000
  image_contents = image_result.content
  image = images.Image(image_contents)
  edited = False
  if image.height > 2000:
    image.resize(width=(image.width * 2000 / image.height), height=2000)
    edited = True
  elif image.width > 4000:
    image.resize(width=4000, height=(image.height * 4000 / image.width))
    edited = True

  if crop_tuple != NO_CROP_TUPLE:
    image.crop(*crop_tuple)
    edited = True

  if edited:
    image_contents = image.execute_transforms(output_encoding=images.JPEG, quality=80)

  # upload with default ACLs set on the bucket  # or use options={'x-goog-acl': 'public-read'})
  gcs_file = gcs.open(image_gcs_path, 'w', content_type='image/jpeg')
  gcs_file.write(image_contents)
  gcs_file.close()

  # thumb
  thumb_gcs_path = CLOUD_STORAGE_BASE_PATH + '/thumbs/' + filename
  thumb = images.Image(image_result.content)
  thumb.resize(width=(thumb.width * THUMB_HEIGHT / thumb.height), height=THUMB_HEIGHT)

  if crop_tuple != NO_CROP_TUPLE:
    thumb.crop(*crop_tuple)
    edited = True

  thumb_contents = thumb.execute_transforms(output_encoding=images.JPEG, quality=40)
  gcs_file = gcs.open(thumb_gcs_path, 'w', content_type='image/jpeg')
  gcs_file.write(thumb_contents)
  gcs_file.close()

  return (CLOUD_STORAGE_ROOT_URL + image_gcs_path,
          CLOUD_STORAGE_ROOT_URL + thumb_gcs_path)


class ServiceAddHandler(BaseBackroomHandler):
  def post(self):
    artwork_json = json.loads(self.request.get('json'))

    publish_date = (datetime.datetime
        .utcfromtimestamp(artwork_json['publishDate'] / 1000)
        .date())
    if FeaturedArtwork.all().filter('publish_date=', publish_date).get() != None:
      webapp2.abort(409, message='Artwork already exists for this date.')

    crop_tuple = tuple(float(x) for x in json.loads(self.request.get('crop')))

    new_image_url, new_thumb_url = maybe_process_image(
        artwork_json['imageUri'],
        crop_tuple,
        publish_date.strftime('%Y%m%d') + ' '
            + artwork_json['title'] + ' '
            + artwork_json['byline'])

    if not new_thumb_url and 'thumbUri' in artwork_json:
      new_thumb_url = artwork_json['thumbUri']
    new_artwork = FeaturedArtwork(
        title=artwork_json['title'],
        byline=artwork_json['byline'],
        attribution=artwork_json['attribution'] if 'attribution' in artwork_json else None,
        image_url=new_image_url,
        thumb_url=new_thumb_url,
        details_url=artwork_json['detailsUri'],
        publish_date=publish_date)
    new_artwork.save()
    self.response.set_status(200)


class ServiceAddFromExternalArtworkUrlHandler(BaseBackroomHandler):
  def post(self):
    publish_date = (datetime.datetime
        .utcfromtimestamp(int(self.request.get('publishDate')) / 1000)
        .date())
    if FeaturedArtwork.all().filter('publish_date =', publish_date).get() != None:
      webapp2.abort(409, message='Artwork already exists for this date.')

    url = self.request.get('externalArtworkUrl')
    result = urlfetch.fetch(url)
    if result.status_code < 200 or result.status_code >= 300:
      webapp2.abort(400, message='Error processing URL: HTTP %d. Content: %s'
          % (result.status_code, result.content))

    soup = BeautifulSoup(result.content)
    attribution = None

    if re.search(r'wikiart.org', url, re.I):
      attribution = 'wikiart.org'
      details_url = re.sub(r'#.+', '', url, re.I | re.S) + '?utm_source=Muzei&utm_campaign=Muzei'
      title = soup.select('h1 span')[0].get_text()
      author = soup.find(itemprop='author').get_text()
      completion_year_el = soup.find(itemprop='dateCreated')
      byline = author + ((', ' + completion_year_el.get_text()) if completion_year_el else '')
      image_url = soup.find(id='paintingImage')['href']
    elif re.search(r'metmuseum.org', url, re.I):
      attribution = 'metmuseum.org'
      details_url = re.sub(r'[#?].+', '', url, re.I | re.S) + '?utm_source=Muzei&utm_campaign=Muzei'
      title = soup.find('h2').get_text()
      author = ''
      try:
        author = unicode(soup.find(text='Artist:').parent.next_sibling).strip()
      except:
        pass
      author = re.sub(r'\s*\(.*', '', author)
      completion_year_el = None
      try:
        completion_year_el = unicode(soup.find(text='Date:').parent.next_sibling).strip()
      except:
        pass
      byline = author + ((', ' + completion_year_el) if completion_year_el else '')
      image_url = soup.find('a', class_='download').attrs['href']
    else:
      webapp2.abort(400, message='Unrecognized URL')

    if not title or not author or not image_url:
      webapp2.abort(500, message='Could not parse HTML')

    image_url, thumb_url = maybe_process_image(image_url,
        NO_CROP_TUPLE,
        publish_date.strftime('%Y%m%d') + ' ' + title + ' ' + byline)

    # create the artwork entry
    new_artwork = FeaturedArtwork(
        title=title,
        byline=byline,
        attribution=attribution,
        image_url=image_url,
        thumb_url=thumb_url,
        details_url=details_url,
        publish_date=publish_date)
    new_artwork.save()

    self.response.set_status(200)
    self.response.out.write(json.dumps(artwork_dict(new_artwork)))


class ServiceEditHandler(BaseBackroomHandler):
  def post(self):
    id = long(self.request.get('id'))
    artwork_json = json.loads(self.request.get('json'))
    crop_tuple = tuple(float(x) for x in json.loads(self.request.get('crop')))
    target_artwork = FeaturedArtwork.get_by_id(id)
    if not target_artwork:
      webapp2.abort(404)

    target_artwork.title = artwork_json['title']
    target_artwork.byline = artwork_json['byline']
    target_artwork.attribution = artwork_json['attribution'] if 'attribution' in artwork_json else None

    new_image_url, new_thumb_url = maybe_process_image(
        artwork_json['imageUri'],
        crop_tuple,
        target_artwork.publish_date.strftime('%Y%m%d') + ' '
            + artwork_json['title'] + ' '
            + artwork_json['byline'])
    if not new_thumb_url and 'thumbUri' in artwork_json:
      new_thumb_url = artwork_json['thumbUri']

    target_artwork.image_url = new_image_url
    target_artwork.thumb_url = new_thumb_url
    target_artwork.details_url = artwork_json['detailsUri']
    target_artwork.save()

    self.response.set_status(200)
    self.response.out.write(json.dumps(artwork_dict(target_artwork)))


class ServiceMoveHandler(BaseBackroomHandler):
  def post(self):
    id = long(self.request.get('id'))
    publish_date = (datetime.datetime
        .utcfromtimestamp(long(self.request.get('publishDate')) / 1000)
        .date())
    target_artwork = FeaturedArtwork.get_by_id(id)
    if not target_artwork:
      webapp2.abort(404)

    # shift other artworks over
    self.move_artwork(target_artwork, publish_date, target_artwork.key().id())
    self.response.set_status(200)

  def move_artwork(self, artwork, publish_date, initial_artwork_id):
    # cascade moves
    current_artwork_at_date = FeaturedArtwork.all().filter('publish_date =', publish_date).get()
    if current_artwork_at_date and current_artwork_at_date.key().id() != initial_artwork_id:
      self.move_artwork(current_artwork_at_date, publish_date + datetime.timedelta(hours=24),
          initial_artwork_id)
    artwork.publish_date = publish_date
    artwork.save()


class ServiceRemoveHandler(BaseBackroomHandler):
  def post(self):
    id = long(self.request.get('id'))
    target_artwork = FeaturedArtwork.get_by_id(id)
    if not target_artwork:
      webapp2.abort(404)

    target_artwork.delete()
    self.response.set_status(200)


class ScheduleHandler(BaseBackroomHandler):
  def get(self):
    self.response.out.write(self.render())

  def render(self):
    return template.render(
        os.path.join(os.path.dirname(__file__), '../templates/backroom_schedule.html'),
        values_with_defaults(dict(
            title='Schedule',
            )))


app = webapp2.WSGIApplication([
    ('/backroom/s/list', ServiceListHandler),
    ('/backroom/s/add', ServiceAddHandler),
    ('/backroom/s/addfromexternal', ServiceAddFromExternalArtworkUrlHandler),
    ('/backroom/s/edit', ServiceEditHandler),
    ('/backroom/s/remove', ServiceRemoveHandler),
    ('/backroom/s/move', ServiceMoveHandler),
    ('/backroom/schedule', ScheduleHandler),
    ],
    debug=IS_DEVELOPMENT)


def main():
  app.run()


if __name__ == '__main__':
  main()