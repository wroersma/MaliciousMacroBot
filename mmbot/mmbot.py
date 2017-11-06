#! /usr/bin/env python

from __future__ import (absolute_import, division,
                        print_function, unicode_literals)
from os import listdir
from os.path import isfile, join
import os
import sys
import time
import pandas as pd
import numpy as np
import re
import hashlib
import logging
import joblib
import gzip
import pkg_resources

if sys.version_info >= (3, 0):
    from oletools.olevba3 import VBA_Parser
else:
    from oletools.olevba import VBA_Parser


from scipy import stats
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.feature_extraction.text import TfidfTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_val_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelBinarizer


class MaliciousMacroBot:
    def __init__(self, benign_path=None, malicious_path=None,
                 model_path=pkg_resources.resource_filename('mmbot', 'model'), retain_sample_contents=False):
        """
        Constructor to setup path variables for model and sample data and initialize object.
        :param benign_path: directory path (relative or absolute) to benign documents for the machine learning model to learn from.
        :param malicious_path: directory path (relative or absolute) to malicious documents for the machine learning model to learn from.
        :param model_path: directory where modeldata.pickle and vocab.txt files are kept.
        :param retain_sample_contents: this relates to level of detail saved in the model data.  If True, potentially sensitive
        information like extracted vba will be stored in the model's pickle file.  The benefit is that incremental
        models can be built, where adding a new file to the training set will result in only reprocessing that one new
        file.  Otherwise all files in the benign_path and malicious_path will be reprocessed each time the model is
        rebuilt.  If you are experimenting with building many models and comparing results, set this to True,
        otherwise keep it to False.
        """
        # os.path.join(os.path.dirname(__file__), 'model')
        self.clear_state()
        self.set_model_paths(benign_path, malicious_path, model_path)
        self.retain_sample_contents = retain_sample_contents

    def clear_state(self):
        """
        Resets object's state to clear out all model internals created after loading state from disk
        """
        self.cls = None
        self.modeldata = None
        self.features = {}

    def set_model_paths(self, benign_path, malicious_path, model_path):
        """
        Helper function to set up paths to files and pre-emptively identify issues with the existence of files and
        paths that will be an issue later.
        :param benign_path: directory path (relative or absolute) to benign documents for the machine learning model to learn from.
        :param malicious_path: directory path (relative or absolute) to malicious documents for the machine learning model to learn from.
        :param model_path: directory where model files and helpful data will be saved for the algorithm to function.
        """

        try:
            # One of the two paths is None
            if (benign_path is None and malicious_path is not None) or (
                            benign_path is not None and malicious_path is None):
                raise IOError("""ERROR: When supplying benign_path and malicious_path, both paths must have samples to
                                 build a classification model.  Either values can be None and an existing saved model
                                 can be supplied, or paths can exist with corresponding office files and a new model
                                 can be built.""")

            # All three paths are None
            if benign_path is None and malicious_path is None and model_path is None:
                raise IOError(
                    "ERROR: All paths supplied for benign_path, malicious_path, and model_path cannot be None")

            # Make sure provided paths actually do exist
            if benign_path is not None and malicious_path is not None:
                self.malicious_path = os.path.join(malicious_path, '')
                if not os.path.exists(malicious_path) or not os.path.isdir(malicious_path):
                    raise IOError("ERROR: The malicious_path provided {} does not exist".format(malicious_path))

                self.benign_path = os.path.join(benign_path, '')
                if not os.path.exists(benign_path) or not os.path.isdir(benign_path):
                    raise IOError("ERROR: The benign_path provided {} does not exist".format(benign_path))

            if model_path is not None:
                self.model_path = os.path.join(model_path, '')
                self.vba_vocab = os.path.join(self.model_path, 'vocab.txt')
                self.modeldata_pickle = os.path.join(self.model_path, 'modeldata.pickle')
                self.modeldata_pickle_gz = os.path.join(self.model_path, 'modeldata.pickle.gz')
                # If the user-supplied path does not exist, use the default vocab.txt that comes with the package
                if not os.path.exists(self.vba_vocab):
                    self.vba_vocab = os.path.join(pkg_resources.resource_filename('mmbot', 'model'), 'vocab.txt')
        except Exception as e:
            raise IOError("ERROR: Supplied benign_path, malicious_path, or model_path does not "
                          "exist or is not a directory.  {}".format(str(e)))

    def get_file_hash(self, pathtofile):
        """
        Computes the MD5 hash of the file
        :param pathtofile: absolute or relative path to a file
        :return: md5 hash of file as a string
        """
        if os.path.isfile(pathtofile):
            with open(pathtofile, 'rb') as file_to_hash:
                filedata = file_to_hash.read()
                md5 = hashlib.md5(filedata).hexdigest()
                # sha1 = hashlib.sha1(filedata).hexdigest()
                # sha256 = hashlib.sha256(filedata).hexdigest()
                return md5
        return None

    def fill_missing_hashes(self, row):
        """
        Checks if there is a null or NaN value for the 'md5' column.  If so, computes it, if not,
        returns original value.  Used to fill in missing md5's in a dataframe.
        :param row: a row of a dataframe with a column named 'md5' and 'filepath'
        :return: for any missing md5 values, computes the hash on the given filepath
        """
        if pd.isnull(row['md5']):
            return self.get_file_hash(row['filepath'])
        else:
            return row['md5']

    def get_file_meta_data(self, filepath, filename=None, getHash=False):
        """
        helper function to get meta information about a file to include it's path, date modified, size
        :param filepath: path to a file
        :param filename: filename
        :param getHash: whether or not the hash should be computed
        :return: a tuple of format (filename, filepath, filesize, filemodified, md5)
        """
        if filename is None:
            filename = os.path.split(filepath)[1]

        filemodified = time.ctime(os.path.getmtime(filepath))
        filesize = os.path.getsize(filepath)
        md5 = np.nan
        if getHash:
            md5 = self.get_file_hash(filepath)
        return (filename, filepath, filesize, filemodified, md5)

    def get_samples_from_disk(self, path=None, getHash=False):
        """
        Given a path to a file or folder of files, recursively lists all files and metadata for the files
        :param path: directory path
        :param getHash: boolean, indicating whether or not to compute hash
        :return: a dataframe with the filename, filepath, filesize, modified date, and md5 hash for each file found
        """
        if not os.path.exists(path):
            raise IOError("ERROR: File or path does not exist: {}".format(path, ))

        if os.path.isfile(path):
            meta = self.get_file_meta_data(path, getHash=getHash)
            return pd.DataFrame({'filename': (meta[0],),
                                 'filepath': (meta[1],),
                                 'filesize': (meta[2],),
                                 'filemodified': (meta[3],),
                                 'md5': (meta[4],)})

        try:
            matches = []
            for root, dirnames, filenames in os.walk(path):
                for filename in filenames:
                    filepath = os.path.join(root, filename)
                    meta = self.get_file_meta_data(filepath, filename, getHash=getHash)
                    matches.append(meta)
            if len(matches) > 0:
                filenames, paths, sizes, dates, md5s = zip(*matches)
                return pd.DataFrame({'filename': filenames, 'filepath': paths, 'filesize': sizes, \
                                     'filemodified': dates, 'md5': md5s})
            return pd.DataFrame()
        except Exception as e:
            raise IOError("ERROR with file or path {}: {}".format(path, str(e)))

    def get_family_name(self, mypath):
        """
        Given a file path, return the deepest directory name to allow organizing samples by name and having that meta
        data in predictions
        :param mypath: path to a file in the model training set
        :return: deepest directory name and 'Unknown' if ther eis a problem with a part of the file path
        """
        normalized_path = os.path.dirname(os.path.abspath(mypath))
        m = re.match(r'.*[\\/](.*?$)', normalized_path)
        try:
            group = m.group(1)
            if len(group) > 0:
                return group
            return 'Unknown'
        except:
            return 'Unknown'

    def new_samples(self, existing, possiblenew):
        """
        Returns dataframe containing rows from possiblenew with MD5 hashes that are not in existing, to identify
        new file samples.
        :param existing: dataframe containing an 'md5' field
        :param possiblenew: dataframe containing an 'md5' field
        :return: Returns dataframe containing rows from possiblenew with MD5 hashes that are not in existing.
        """
        existing_items = existing['md5'].tolist()
        possiblenew_items = possiblenew['md5'].tolist()
        actualnew_items = [x for x in possiblenew_items if x not in existing_items]
        if len(actualnew_items) > 0:
            return possiblenew[possiblenew['md5'].isin(actualnew_items)].copy()
        return None


    def get_language_features(self):
        """
        After vba has been extracted from all files, this function does feature extraction on that vba and prepares
        everything for a model to be built.  load_model_data has been called, populating self.modeldata
        :return: feature matrix and labels in a dictionary structure with keys 'X' and 'y' respectively
        """
        self.load_model_vocab()

        # Get custom VBA features
        self.modeldata = pd.concat([self.modeldata, self.modeldata.extracted_vba.apply(self.get_vba_features)], axis=1)

        tempfeatures = self.modeldata.columns
        self.features['vba_features'] = [x for x in tempfeatures if x.startswith('vba_')]

        # Count Vectorizer
        vocab_lower = [x.lower() for x in self.features['vocab']]
        vocab_lower = list(set(vocab_lower))
        self.model_cntvect = CountVectorizer(vocabulary=vocab_lower,
                                             lowercase=True,
                                             decode_error='ignore',
                                             token_pattern=r"(?u)\b\w[\w\.]+\b")
        self.modeldata_cnts = self.model_cntvect.fit_transform(self.modeldata['extracted_vba'])
        self.features['cnt_features'] = ['cnt_' + x for x in self.model_cntvect.get_feature_names()]
        self.features['features'] = self.model_cntvect.get_feature_names()
        self.modeldata = self.modeldata.join(pd.DataFrame(self.modeldata_cnts.toarray(),
                                                          columns=self.features['cnt_features']))

        # TF-IDF Transformer
        self.model_tfidf_trans = TfidfTransformer()
        self.model_tfidf_cntvect = self.model_tfidf_trans.fit_transform(self.modeldata_cnts.toarray())
        self.features['tfidf_features'] = ['tfidf_' + x for x in self.features['features']]
        self.modeldata = self.modeldata.join(pd.DataFrame(self.model_tfidf_cntvect.toarray(),
                                                          columns=self.features['tfidf_features']))

        # Train and Test Model
        predictive_features = self.features['tfidf_features'] + self.features['vba_features']

        self.features['predictive_features'] = predictive_features
        self.clf_X = self.modeldata[predictive_features].as_matrix()
        self.clf_y = np.array(self.modeldata['label'])

        return {'X': self.clf_X, 'y': self.clf_y}

    def clear_model_features(self):
        """
        Removes all columns from modeldata with names starting with cnt_, tfidf_, or vba_
        These are the computed columns for the model
        """
        if self.modeldata is not None:
            columns = self.modeldata.columns
            cntcolumns = [x for x in columns if x.startswith('cnt_')]
            vba_feature_columns = [x for x in columns if x.startswith('vba_')]
            tfidfcolumns = [x for x in columns if x.startswith('tfidf_')]
            self.modeldata.drop(self.modeldata[cntcolumns], axis=1, inplace=True)
            self.modeldata.drop(self.modeldata[vba_feature_columns], axis=1, inplace=True)
            self.modeldata.drop(self.modeldata[tfidfcolumns], axis=1, inplace=True)

    def build_models(self):
        """
        After get_language_features is called, this function builds the models based on
        the classifier matrix and labels.
        :return:
        """
        self.cls = RandomForestClassifier(n_estimators=100, max_features=.2)
        # build classifier
        self.cls.fit(self.clf_X, self.clf_y)

        return self.cls

    def load_model_vocab(self):
        """
        Loads vocabulary used in the bag of words model
        :return: fixed vocabulary that was loaded into internal state
        """
        with open(self.vba_vocab) as vocabfile:
            lines = vocabfile.readlines()
            lines = [x.strip() for x in lines]
        self.features['vocab'] = set(lines)
        return self.features['vocab']

    def load_model_data(self, exclude=None):
        """
        Merges previously saved model data (if exists) with new files found in malicious and benign doc paths.
        :param exclude: string value - if samples (including path) from the training set contain this string,
        they will be omitted from the model.  This is primarily used to hold malware families from consideration
        in the model to assess classification generalization to new unknown families.
        :return: number of new documents loaded into the model
        """
        newdoc_cnt = 0

        knowndocs = None
        # Clear all stored contents because we don't save enough detail to pick up where we left off last time
        if self.modeldata is not None:
            knowndocs = self.modeldata.copy(deep=True)

        maldocs = self.get_samples_from_disk(self.malicious_path)
        if len(maldocs) > 0:
            maldocs['label'] = 'malicious'

        benigndocs = self.get_samples_from_disk(self.benign_path)
        if len(benigndocs) > 0:
            benigndocs['label'] = 'benign'

        if len(benigndocs) == 0 and len(maldocs) == 0 and knowndocs is None:
            raise IOError("ERROR: Unable to load saved model data {} or process samples rooted in model path {}.  "
                          "Unable to make predictions.".format(self.modeldata_pickle, self.model_path))


        possiblenew = pd.concat([maldocs, benigndocs], axis=0)

        if knowndocs is None:
            # No existing samples, so alldocs are newly found docs.
            possiblenew['md5'] = possiblenew['filepath'].apply(self.get_file_hash)
            possiblenew[['extracted_vba', 'stream_path', 'filename_vba']] = possiblenew['filepath'].apply(self.get_vba)
            possiblenew['family'] = possiblenew['filepath'].apply(self.get_family_name)
            alldocs = possiblenew
            newdoc_cnt = len(alldocs)
        else:
            temp = knowndocs.append(possiblenew)
            # Omit seemingly duplicate files with same filepath, filesize and modified date
            grouped_rows = temp.groupby(['filesize', 'filepath', 'filemodified'])
            omit = grouped_rows.filter(lambda x: len(x) > 1)['filepath'].unique()
            temp = temp[~((temp['filepath'].isin(omit)) & temp['md5'].isnull())].reset_index(drop=True)
            # Compute hashes for those that are new.  Omit files with duplicate md5 hashes
            temp['md5'] = temp.apply(self.fill_missing_hashes, axis=1)
            temp = temp.drop_duplicates(subset='md5', keep='first')
            temp.reset_index(drop=True)

            newdocs = temp[temp['extracted_vba'].isnull()].copy()
            knowndocs = temp[~temp['extracted_vba'].isnull()].copy()

            # get enrichment for truly new docs
            if len(newdocs) > 0:
                logging.info("%d NEW DOCS FOUND!" % (len(newdocs),))
                logging.info(newdocs[['filename', 'filemodified', 'filesize', 'filepath']])
                newdocs[['extracted_vba', 'stream_path', 'filename_vba']] = newdocs['filepath'].apply(self.get_vba)
                newdoc_cnt = len(newdocs)
                newdocs['family'] = newdocs['filepath'].apply(self.get_family_name)
                alldocs = pd.concat([knowndocs, newdocs], axis=0)
                alldocs.reset_index(drop=True, inplace=True)

            else:
                logging.warning("No new model data found")
                alldocs = knowndocs

        # keep only what we'll be working with
        if exclude is not None:
            self.modeldata = alldocs.drop(alldocs[alldocs['filepath'].str.contains(exclude)].index)
        else:
            self.modeldata = alldocs

        return newdoc_cnt

    def save_model(self):
        """
        Saves all necessary model state information for classification work to disk.
        :return: True if it succeeded and False otherwise.
        """
        # if we aren't keeping the extracted file details to reproduce the analysis, let's clear that data and
        # save the model.  It's not needed to perform basic predictions on new files.
        if self.retain_sample_contents is False:
            metadata = {'filemodified', 'extracted_vba', 'filename_vba', 'filepath', 'filename', 'function_names',
                        'filesize', 'filemodified', 'stream_path'}
            metadata_delete = list(metadata & set(self.modeldata.columns))
            self.modeldata.drop(metadata_delete, axis=1, inplace=True)

        try:
            saved_model = {'modeldata': self.modeldata,
                           'features': self.features,
                           'model_cntvect_cnts_array': self.modeldata_cnts.toarray()
                           }

            joblib.dump(saved_model, self.modeldata_pickle)

        except Exception as e:
            raise IOError("Error saving model data to disk: {}".format(str(e)))
            return False
        return True

    def load_model(self):
        """
        This function attempts to load the model from json file
        :return: True for success, False for failure
        """
        load_model = None
        try:
            load_model = joblib.load(self.modeldata_pickle)
            logging.info("Using extracted modeldata.pickle file")
        except IOError:
            try:
                logging.info("Using compressed modeldata.pickle.gz file"
                             "this may be slower than using modeldata.pickle ")
                load_model = joblib.load(gzip.open(self.modeldata_pickle_gz))
            except Exception as x:
                logging.warning("Could not load model from pickle file {}.\nFailed with error: {}"
                                .format(self.modeldata_pickle, str(x)))
        except TypeError as y:
            logging.error("Pickle file may be corrupted, please verify you have a proper pickle file {}".format(str(y)))
        try:
            self.modeldata = load_model['modeldata']
            self.features = load_model['features']
            self.model_cntvect_cnts_array = load_model['model_cntvect_cnts_array']
            self.vocab = self.features['vocab']

            # Count Vectorizer
            vocab_lower = [x.lower() for x in self.features['vocab']]
            vocab_lower = list(set(vocab_lower))
            self.model_cntvect = CountVectorizer(vocabulary=vocab_lower,
                                                 lowercase=True,
                                                 decode_error='ignore',
                                                 token_pattern=r"(?u)\b\w[\w\.]+\b")

            # TF-IDF Transformer
            self.model_tfidf_trans = TfidfTransformer()
            self.model_tfidf_cntvect = self.model_tfidf_trans.fit_transform(self.model_cntvect_cnts_array)

            # Train and Test Model
            predictive_features = self.features['tfidf_features'] + self.features['vba_features']

            self.features['predictive_features'] = predictive_features
            self.clf_X = self.modeldata[predictive_features].as_matrix()
            self.clf_y = np.array(self.modeldata['label'])

            self.build_models()

            if 'filename' not in self.modeldata.columns:
                self.modeldata['filename'] = 'none'
            if 'filemodified' not in self.modeldata.columns:
                self.modeldata['filemodified'] = 'none'
            if 'filepath' not in self.modeldata.columns:
                self.modeldata['filepath'] = 'none'
            if 'filesize' not in self.modeldata.columns:
                self.modeldata['filesize'] = 'none'

        except Exception as e:
            logging.error("Error loading model {}\nError: {}".format(self.modeldata_pickle, str(e)))
            return False

        return True

    def get_vba(self, myfile, source='filepath'):
        """
        Given a file, parses out the stream paths, vba code, and vba filenames for each.
        :param myfile: filename
        :param source: type of data being passed in.  Either "filepath" to indicate we need to read from disk or
        "filecontents" meaning that the file contents are being passed as a parameter.
        :return: pandas Series that can be used in concert with the pandas DataFrame apply method
        """
        if source == 'filepath':
            filedata = open(myfile, 'rb').read()
        else:
            filedata = myfile

        try:
            vbaparser = VBA_Parser('mmbot', data=filedata)
            pathnames = ''
            if vbaparser.detect_vba_macros():
                filenameslist = []
                pathnameslist = []
                vbacodelist = []
                for (filename, stream_path, filename_vba, extracted_vba) in vbaparser.extract_macros():
                    vbacodelist.append(extracted_vba.decode("ascii", "ignore"))

                    if pathnames is None:
                        pathnameslist.append(stream_path.decode("ascii", "ignore"))
                        filenameslist.append(filename_vba.decode("ascii", "ignore"))
                    else:
                        pathnameslist.append(stream_path.decode("ascii", "ignore"))
                        filenameslist.append(filename_vba.decode("ascii", "ignore"))
                allcode = "\n\n\n\n".join(vbacodelist)
                filenames = ", ".join(filenameslist)
                pathnames = ", ".join(pathnameslist)

            else:
                pathnames = 'No VBA Macros found'
                filenames = 'No VBA Macros found'
                allcode = 'No VBA Macros found'

        except Exception as e:
            pathnames = 'Error:' + str(e)
            filenames = 'Error:' + str(e)
            allcode = 'Error:' + str(e)

        return pd.Series({'extracted_vba': allcode, 'stream_path': pathnames, 'filename_vba': filenames})

    def get_entropy(self, vbcodeSeries):
        """
        Helper function to return entropy calculation value
        :param vbcodeSeries: pandas series of values
        :return: entropy of the set of values.
        """
        probs = vbcodeSeries.value_counts() / len(vbcodeSeries)
        entropy = stats.entropy(probs)
        return entropy

    def get_vba_features(self, vb):
        """
        Given VB code as a string input, returns various summary data about it.
        :param vb: vbacode as one large multiline string
        :return: pandas Series that can be used in concert with the pandas DataFrame apply method
        """
        allfunctions = []
        all_num_functions = []
        all_locs = []
        entropy_func_names = 0
        avg_param_per_func = 0.0
        functions_str = ''
        vba_cnt_func_loc_ratio = 0.0
        vba_cnt_comment_loc_ratio = 0.0

        if vb == 'No VBA Macros found' or vb[0:6] == 'Error:':
            functions = 'None'
            num_functions = 0
            loc = 0
            avg_loc_func = 0
            num_comments = 0
            entropy_chars = 0
            entropy_words = 0
        else:
            functions = {}
            num_comments = vb.count("'")
            lines = vb.splitlines()
            new_lines = []
            num_functions = 0
            entropy_chars = self.get_entropy(pd.Series(vb.split(' ')))
            entropy_words = self.get_entropy(pd.Series(list(vb)))
            reFunction = re.compile(r'.*\s?[Sub|Function]\s+([a-zA-Z0-9_]+)\((.*)\)')
            for line in lines:
                if len(line.strip()) > 0:
                    new_lines.append(line)

                function_name_matches = reFunction.findall(line)
                num_params = 0
                if len(function_name_matches) > 0:
                    num_functions = num_functions + 1
                    num_params = function_name_matches[0][1].count(',') + 1
                    if len(function_name_matches[0][1].strip()) <= 0:
                        num_params = 0
                    functions[function_name_matches[0][0]] = num_params

            loc = len(new_lines)
            if len(functions) > 0:
                function_name_str = ''.join(functions.keys())
                entropy_func_names = self.get_entropy(pd.Series(list(function_name_str)))
                functions_str = ', '.join(functions.keys())
                param_list = functions.values()
                avg_param_per_func = (1.0 * sum(param_list)) / len(param_list)
            if loc > 0:
                vba_cnt_func_loc_ratio = (1.0 * len(functions)) / loc
                vba_cnt_comment_loc_ratio = (1.0 * num_comments) / loc
            if num_functions <= 0:
                avg_loc_func = float(loc)
            else:
                avg_loc_func = float(loc) / num_functions

        return pd.Series({'function_names': functions_str,
                          'vba_avg_param_per_func': avg_param_per_func,
                          'vba_cnt_comments': num_comments,
                          'vba_cnt_functions': num_functions,
                          'vba_cnt_loc': loc,
                          'vba_cnt_func_loc_ratio': vba_cnt_func_loc_ratio,
                          'vba_cnt_comment_loc_ratio': vba_cnt_comment_loc_ratio,
                          'vba_entropy_chars': entropy_chars,
                          'vba_entropy_words': entropy_words,
                          'vba_entropy_func_names': entropy_func_names,
                          'vba_mean_loc_per_func': avg_loc_func
                          })

    def get_top_vba_features(self, sample, top=5):
        """
        Given a sample dataframe, identifies and returns the top VBA features ranking and counts that
        contributed to the prediction.  This includes the "featureprint".
        :param sample: dictionary result from a classification prediction
        :param top: number of ranked features to return.
        :return: returns a dictionary of the top VBA features ranking and counts that
        contributed to the prediction.
        """
        relevantFeatures = []
        nonzero_tfidf_features = np.array(sample[self.features['tfidf_features']]).nonzero()
        sample_tfidf_features_row = np.array(sample[self.features['tfidf_features']])[0]
        sample_cnt_row = np.array(sample[self.features['cnt_features']])

        # Collect information for all features that helped with the prediction
        for i in nonzero_tfidf_features[1]:
            feature_name = (self.features['tfidf_features'][i])
            feature_value = sample_tfidf_features_row[i]
            if feature_name.startswith("tfidf_"):
                feature_cnt = sample[feature_name.replace("tfidf_", "cnt_")].iloc[0]
            else:
                feature_cnt = feature_value
            feature_name = feature_name.replace("tfidf_", "")
            relevantFeatures.append((feature_name, feature_value, feature_cnt))

        # Sort all features that aided in prediction by their relative importance
        result = sorted(relevantFeatures, key=lambda x: x[1], reverse=True)

        if top >= len(result):
            top = len(result) - 1
        flat_top_features = {}
        names = {'feat_' + str(x) + '_name': result[x][0] for x in range(1, (top + 1))}
        importance = {'feat_' + str(x) + '_importance': result[x][1] for x in range(1, (top + 1))}
        counts = {'feat_' + str(x) + '_cnt': result[x][2] for x in range(1, (top + 1))}

        nested_top_features = []
        for x in range(1, (top + 1)):
            nested_top_features.append({'name': result[x][0],
                                        'importance': int(round(100 * result[x][1])),
                                        'cnt': result[x][2],
                                        })

        sorted_names = sorted(names.keys())
        flat_top_features['featureprint'] = '_'.join([str(names[y]) for y in sorted_names])
        nested_top_features = '_'.join([names[y] for y in sorted_names])
        flat_top_features.update(names)
        flat_top_features.update(importance)
        flat_top_features.update(counts)

        return (flat_top_features, nested_top_features)

    def classify_vba(self, vba):
        """
        Applies classification model for prediction and clustering related samples to
        vba input provided as a pandas Series.
        :param vba: extracted VBA
        :return: results as a pandas Series
        """
        sample = pd.DataFrame(data=[vba], columns=['extracted_vba'])
        extracted_vba_array = sample['extracted_vba']
        newsample_cnt = self.model_cntvect.transform(extracted_vba_array).toarray()
        newsample_tfidf = self.model_tfidf_trans.transform(newsample_cnt).toarray()
        newsample_df = pd.DataFrame(self.get_vba_features(vba)).T

        predictive_features = self.features['tfidf_features'] + self.features['vba_features']

        # Join all features for this sample into one dataframe
        newsample_df_cnt = pd.DataFrame(newsample_cnt, columns=self.features['cnt_features'])
        newsample_df_tfidf = pd.DataFrame(newsample_tfidf, columns=self.features['tfidf_features'])

        newsample_df = newsample_df.join(newsample_df_cnt)
        newsample_df = newsample_df.join(newsample_df_tfidf)

        newsample = newsample_df[predictive_features].as_matrix()
        prediction = self.cls.predict(newsample)
        proba = self.cls.predict_proba(newsample)

        # Assemble results as a flat dictionary and nested dictionary
        vba_feature_results = self.get_top_vba_features(newsample_df, top=5)
        flat_result_dictionary = vba_feature_results[0]

        nested_dictionary = {'vba_lang_features': vba_feature_results[1]}

        for feature in self.features['vba_features']:
            flat_result_dictionary[feature] = newsample_df[feature].iloc[0]
            if isinstance(newsample_df[feature].iloc[0], (np.float64, float)):
                nested_dictionary[feature] = round(newsample_df[feature].iloc[0], 2)
            else:
                nested_dictionary[feature] = newsample_df[feature].iloc[0]

        nested_dictionary['function_names'] = newsample_df['function_names'].iloc[0]
        nested_dictionary['prediction'] = prediction[0]
        nested_dictionary['confidence'] = round(proba.max(), 2)

        flat_result_dictionary['function_names'] = newsample_df['function_names'].iloc[0]
        flat_result_dictionary['prediction'] = prediction[0]
        flat_result_dictionary['result_dictionary'] = nested_dictionary
        flat_result_dictionary['confidence'] = round(proba.max(), 2)

        return pd.Series(flat_result_dictionary)

    def mmb_init_model(self, modelRebuild=False, exclude=None, labeled_df=None):
        """
        Initiates the machine learning models used in order to begin making predictions.

        :param modelRebuild: boolean used to rebuild the model by looking for new samples
        on disk or just load the old model without checking for new samples.  If no
        saved models are found, it will attempt to rebuild from samples in the model directories.
        :param exclude: if samples used in the model contain this string,
        they will be omitted from the model.  This is primarily used to hold malware
        families from consideration in the model to test the algorithm for classification generalization
        to unknown families and techniques.
        :param labeled_df: used mostly internally to test out new models.  If the dataframe has
        at least columns for ['label', 'extracted_vba', 'md5'], then this dataframe will be used
        to rebuild the model.
        :return: True if successful and False otherwise.
        """
        if labeled_df is not None:
            self.modeldata = labeled_df
            self.clear_model_features()
            self.get_language_features()
            self.build_models()
            modelsLoaded = True
        else:
            modelsLoaded = self.load_model()
            if modelRebuild or not modelsLoaded:
                newdoc_cnt = self.load_model_data(exclude)
                if newdoc_cnt > 0:
                    self.clear_model_features()
                    self.get_language_features()
                    self.build_models()
                    modelsLoaded = self.save_model()
                if (self.modeldata is None) or (len(self.modeldata) == 0):
                    logging.error("""No model data found, supervised machine learning requires
                                     labeled samples.  Check that samples exist in the benign_samples and
                                     malicious_samples directories and that existing model files with .pickle
                                     extensions exist in the existsmodels""")
                    modelsLoaded = False
        return modelsLoaded

    def mmb_evaluate_model(self):
        """
        Returns scores from cross validation evaluation on the malicious / benign classifier
        """
        predictive_features = self.features['predictive_features']
        self.clf_X = self.modeldata[predictive_features].as_matrix()
        self.clf_y = np.array(self.modeldata['label'])

        X_train, X_test, y_train, y_test = train_test_split(self.clf_X, self.clf_y, test_size=0.2, random_state=0)
        lb = LabelBinarizer()
        y_train = np.array([number[0] for number in lb.fit_transform(y_train)])
        eval_cls = RandomForestClassifier(n_estimators=100, max_features=.2)
        eval_cls.fit(X_train, y_train)

        recall = cross_val_score(eval_cls, X_train, y_train, cv=5, scoring='recall')
        precision = cross_val_score(eval_cls, X_train, y_train, cv=5, scoring='precision')
        accuracy = cross_val_score(eval_cls, X_train, y_train, cv=5, scoring='accuracy')
        f1_score = cross_val_score(eval_cls, X_train, y_train, cv=5, scoring='f1_macro')

        return {'accuracy': accuracy, 'f1': f1_score, 'precision': precision, 'recall': recall}

    def mmb_predict(self, sample_input, datatype='filepath', exclude_files=None):
        """
        Given a suspicious office file input, make a prediction on whether it is benign or malicious
        and provide featureprint and key statistics.
        :param sample_input:         sample_input is the input to be used in the prediction.  It may be:
          - a python string of already extracted VBA
          - a file read into a buffer (e.g. with the open().read() with the 'rb' flag), which is of type str
          - a directory path to a specific file or directory containing many files to be classified
          - a pandas DataFrame containing any of the three scenarios listed above and column names of either 'filepath', 'filecontents', or 'extracted_vba'
        :param datatype: a string indicating the type of information in the sample_input field and must be one of the
        following three values 'vba', 'filecontents', or 'filepath'.
        :param exclude_files: if any of the file paths and file names contain the string provided, they will not be analyzed.
        :return: Returns a 'dataframe' with the prediction results
        """
        if not isinstance(sample_input, (str, pd.DataFrame)):
            raise TypeError("sample_input must be either a string or pandas DataFrame")
        if len(sample_input) <= 0:
            return pd.DataFrame()

        sample = None
        if datatype == 'filepath':
            if isinstance(sample_input, str):
                sample = self.get_samples_from_disk(sample_input, getHash=True)
            if isinstance(sample_input, pd.DataFrame):
                if 'filepath' not in sample_input.columns:
                    raise ValueError("DataFrame must contain a column named 'filepath'")

                sample = pd.DataFrame()
                allfiles = []
                for i in range(len(sample_input)):
                    morefiles = self.get_samples_from_disk(sample_input.iloc[i]['filepath'], getHash=True)
                    allfiles.append(morefiles)
                sample = sample.append(allfiles)
            if exclude_files is not None:
                sample = sample.drop(sample[sample['filepath'].str.contains(exclude_files)].index)
            sample = pd.concat([sample, sample.filepath.apply(self.get_vba)], axis=1)
        if datatype == 'filecontents':
            if isinstance(sample_input, str):
                sample_vba = self.get_vba(sample_input, source=datatype)
                sample = pd.DataFrame([sample_vba])
            if isinstance(sample_input, pd.DataFrame):
                if 'filecontents' not in sample_input.columns:
                    raise ValueError("DataFrame must contain a column named 'filecontents'")

                sample = pd.concat([sample_input, sample_input.filecontents.apply(self.get_vba, args=(datatype,))],
                                   axis=1)
        if datatype == 'vba':
            if isinstance(sample_input, str):
                sample = pd.DataFrame(data=[sample_input], columns=['extracted_vba'])
            if isinstance(sample_input, pd.DataFrame):
                if 'extracted_vba' not in sample_input.columns:
                    raise ValueError("DataFrame must contain a column named 'extracted_vba'")
                sample = sample_input
        if sample is not None and len(sample) > 0:
            complete_result = pd.concat([sample, sample.extracted_vba.apply(self.classify_vba)], axis=1)
            # if no macros were found, then predict benign
            complete_result.loc[complete_result.extracted_vba == 'No VBA Macros found', 'prediction'] = 'benign'
            return complete_result
        else:
            raise ValueError("Unexpected error occurred.")

    def mmb_prediction_to_json(self, prediction):
        """
        Given a prediction DataFrame obtained from calling mmb_predict() convert primary fields into
        a dict that can be easily converted to a search-friendly json representation for a technology like a
        No-SQL database or technology like Elasticsearch.
        :param prediction: result of mmb_predict
        :return: a dictionary of statistics and classification results for the sample
        """
        array = []
        if not isinstance(prediction, pd.DataFrame):
            raise ValueError("prediction parameter must be a DataFrame with a column named 'result_dictionary'")

        if 'result_dictionary' not in prediction.columns:
            raise ValueError("DataFrame must contain a column named 'extracted_vba'")

        for i in range(len(prediction)):
            array.append(prediction.iloc[i]['result_dictionary'])
        return array
