""" Tests for package storage backends """
import time
from six import StringIO
from datetime import datetime

import shutil
import tempfile
try:
    from mock import MagicMock, patch
except ImportError:
    from unittest.mock import MagicMock, patch
from moto import mock_s3
try:
    from urlparse import urlparse, parse_qs
except ImportError:
    from urllib.parse import urlparse, parse_qs

import boto
import os
import pypicloud
import re
from boto.s3.key import Key
import boto.exception
from pypicloud.models import Package
from pypicloud.storage import S3Storage, FileStorage
from . import make_package

try:
    import unittest2 as unittest  # pylint: disable=F0401
except ImportError:
    import unittest


class TestS3Storage(unittest.TestCase):

    """ Tests for storing packages in S3 """

    def setUp(self):
        super(TestS3Storage, self).setUp()
        self.s3_mock = mock_s3()
        self.s3_mock.start()
        self.settings = {
            'storage.bucket': 'mybucket',
            'storage.access_key': 'abc',
            'storage.secret_key': 'bcd',
        }
        conn = boto.connect_s3()
        self.bucket = conn.create_bucket('mybucket')
        patch.object(S3Storage, 'test', True).start()
        kwargs = S3Storage.configure(self.settings)
        self.storage = S3Storage(MagicMock(), **kwargs)

    def tearDown(self):
        super(TestS3Storage, self).tearDown()
        patch.stopall()
        self.s3_mock.stop()

    def test_list(self):
        """ Can construct a package from a S3 Key """
        key = Key(self.bucket)
        name, version, filename = 'mypkg', '1.2', 'pkg.tar.gz'
        key.key = name + '/' + filename
        key.set_metadata('name', name)
        key.set_metadata('version', version)
        key.set_contents_from_string('foobar')
        package = list(self.storage.list(Package))[0]
        self.assertEquals(package.name, name)
        self.assertEquals(package.version, version)
        self.assertEquals(package.filename, filename)

    def test_list_no_metadata(self):
        """ Test that list works on old keys with no metadata """
        key = Key(self.bucket)
        name, version = 'mypkg', '1.2'
        filename = '%s-%s.tar.gz' % (name, version)
        key.key = name + '/' + filename
        key.set_contents_from_string('foobar')
        package = list(self.storage.list(Package))[0]
        self.assertEquals(package.name, name)
        self.assertEquals(package.version, version)
        self.assertEquals(package.filename, filename)

    def test_get_url(self):
        """ Mock s3 and test package url generation """
        package = make_package()
        response = self.storage.download_response(package)

        parts = urlparse(response.location)
        self.assertEqual(parts.scheme, 'https')
        self.assertEqual(parts.netloc, 'mybucket.s3.amazonaws.com')
        self.assertEqual(parts.path, '/' + self.storage.get_path(package))
        query = parse_qs(parts.query)
        self.assertItemsEqual(query.keys(), ['Expires', 'Signature',
                                             'AWSAccessKeyId'])
        self.assertTrue(int(query['Expires'][0]) > time.time())
        self.assertEqual(query['AWSAccessKeyId'][0],
                         self.settings['storage.access_key'])

    def test_delete(self):
        """ delete() should remove package from storage """
        package = make_package()
        self.storage.upload(package, StringIO())
        self.storage.delete(package)
        keys = list(self.bucket.list())
        self.assertEqual(len(keys), 0)

    def test_upload(self):
        """ Uploading package sets metadata and sends to S3 """
        package = make_package()
        datastr = 'foobar'
        data = StringIO(datastr)
        self.storage.upload(package, data)
        key = list(self.bucket.list())[0]
        self.assertEqual(key.get_contents_as_string(), datastr)
        self.assertEqual(key.get_metadata('name'), package.name)
        self.assertEqual(key.get_metadata('version'), package.version)

    def test_upload_prepend_hash(self):
        """ If prepend_hash = True, attach a hash to the file path """
        self.storage.prepend_hash = True
        package = make_package()
        data = StringIO()
        self.storage.upload(package, data)
        key = list(self.bucket.list())[0]

        pattern = r'^[0-9a-f]{4}/%s/%s$' % (re.escape(package.name),
                                            re.escape(package.filename))
        match = re.match(pattern, key.key)
        self.assertIsNotNone(match)

    @patch.object(pypicloud.storage.s3, 'boto')
    def test_create_bucket(self, boto_mock):
        """ If S3 bucket doesn't exist, create it """
        conn = boto_mock.s3.connect_to_region()
        boto_mock.exception.S3ResponseError = boto.exception.S3ResponseError

        def raise_not_found(*_, **__):
            """ Raise a 'bucket not found' exception """
            e = boto.exception.S3ResponseError(400, 'missing')
            e.error_code = 'NoSuchBucket'
            raise e
        conn.get_bucket = raise_not_found
        settings = {
            'storage.bucket': 'new_bucket',
            'storage.region': 'us-east-1',
        }
        S3Storage.configure(settings)
        conn.create_bucket.assert_called_with('new_bucket',
                                              location='us-east-1')


class TestFileStorage(unittest.TestCase):

    """ Tests for storing packages as local files """

    def setUp(self):
        super(TestFileStorage, self).setUp()
        self.tempdir = tempfile.mkdtemp()
        settings = {
            'storage.dir': self.tempdir,
        }
        kwargs = FileStorage.configure(settings)
        self.request = MagicMock()
        self.storage = FileStorage(self.request, **kwargs)

    def tearDown(self):
        super(TestFileStorage, self).tearDown()
        shutil.rmtree(self.tempdir)

    def test_upload(self):
        """ Uploading package saves file """
        package = make_package()
        datastr = 'foobar'
        data = StringIO(datastr)
        self.storage.upload(package, data)
        filename = self.storage.get_path(package)
        self.assertTrue(os.path.exists(filename))
        with open(filename, 'r') as ifile:
            self.assertEqual(ifile.read(), 'foobar')

    def test_list(self):
        """ Can iterate over uploaded packages """
        package = make_package()
        path = self.storage.get_path(package)
        os.makedirs(os.path.dirname(path))
        with open(path, 'w') as ofile:
            ofile.write('foobar')

        pkg = list(self.storage.list(Package))[0]
        self.assertEquals(pkg.name, package.name)
        self.assertEquals(pkg.version, package.version)
        self.assertEquals(pkg.filename, package.filename)

    def test_delete(self):
        """ delete() should remove package from storage """
        package = make_package()
        path = self.storage.get_path(package)
        os.makedirs(os.path.dirname(path))
        with open(path, 'w') as ofile:
            ofile.write('foobar')
        self.storage.delete(package)
        self.assertFalse(os.path.exists(path))

    def test_create_package_dir(self):
        """ configure() will create the package dir if it doesn't exist """
        tempdir = tempfile.mkdtemp()
        os.rmdir(tempdir)
        settings = {
            'storage.dir': tempdir,
        }
        FileStorage.configure(settings)
        try:
            self.assertTrue(os.path.exists(tempdir))
        finally:
            os.rmdir(tempdir)
