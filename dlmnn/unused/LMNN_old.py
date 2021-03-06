#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Feb 16 08:50:54 2018

@author: nsde
"""

#%% Packages to use
from __future__ import print_function
import tensorflow as tf
import os
import numpy as np
import datetime
from sklearn.neighbors import NearestNeighbors, KNeighborsClassifier
from sklearn.decomposition import PCA

from dlmnn.helper.tf_funcs import tf_makePairwiseFunc
from dlmnn.helper.utility import get_optimizer, progressBar
from dlmnn.helper.neighbor_funcs import _weight_func as weight_func
from dlmnn.helper.logger import stat_logger


#%% Main Class
class lmnn(object):
    def __init__(self, tf_transformer, margin=1, session=None, dir_loc=None,
                 optimizer='adam', verbose = 1):
        ''' Class for running the Large Margin Nearest Neighbour algorithm. 
            
        Arguments:
            
            tf_transformer: 
                a callable function that takes a single i.e:
                X_trans = tf_transformer(X). It is this function that is optimized
                during training, and should therefore include some trainable
                parameters
            
            margin: 
                margin threshold for the algorithm. Determines the scaling of the
                feature space
            
            session: 
                tensorflow session which the computations are runned within. 
                If None then a new is opened
            
            dir_loc: 
                directory to store tensorboard files. If None, a folder
                will be created named lmnn
            
            optimizer: 
                str, which optimizer to use for the training
            
            verbose: 
                integer, in range [0,2], controls the level of output
                
        '''
        # Initilize session and tensorboard dirs 
        self.trans_name = tf_transformer.__name__
        self.session = tf.Session() if session is None else session
        self.dir_loc = (dir_loc+'/'+self.trans_name) if dir_loc is not None \
                        else self.trans_name
        self.train_writer = None
        self.val_writer = None
        
        # Set variables for later training
        self.optimizer = get_optimizer(optimizer)
        self.verbose = verbose
        self.margin = margin

        # Set transformer and create a pairwise distance metric function
        self.transformer = tf_transformer
        self.metric_func = tf_makePairwiseFunc(tf_transformer)
    
    def tf_findImposters(self, X, y, tN, margin=None):
        ''' Function for finding imposters in LMNN
            For a set of observations X and that sets target neighbours in tN, 
            find all points that violate the following two equations
                    D(i, imposter) <= D(i, target_neighbour) + 1,
                    y(imposter) == y(target_neibour)
            for a given distance measure
            
        Arguments:
            X: N x ? matrix or tensor of data
            
            y: N x 1 vector, with class labels
            
            L: d x d matrix, mahalanobis parametrization where M = L^T*L
            
            tN: (N*k) x 2 matrix, where the first column in each row is the
                observation index and the second column is the index of one
                of the k target neighbours
        Output:
            tup: M x 3, where M is the number of triplets that where found to
                 fullfill the imposter equation. First column in each row is the 
                 observation index, second column is the target neighbour index
                 and the third column is the imposter index
        '''
        with tf.name_scope('findImposters'):
            margin = self.margin if margin is None else margin
            N = tf.shape(X)[0]
            n_tN = tf.shape(tN)[0]
            
            # Calculate distance
            D = self.metric_func(X, X) # d x d
            
            # Create all combination of [points, targetneighbours, imposters]
            possible_imp_array =  tf.expand_dims(tf.reshape(
                tf.ones((n_tN, N), tf.int32)*tf.range(N), (-1, )), 1)
            tN_tiled = tf.reshape(tf.tile(tN, [1, N]), (-1, 2))
            full_idx = tf.concat([tN_tiled, possible_imp_array], axis=1)
            
            # Find distances for all combinations
            tn_index = full_idx[:,:2]
            im_index = full_idx[:,::2]
            D_tn = tf.gather_nd(D, tn_index)
            D_im = tf.gather_nd(D, im_index)
            
            # Find actually imposter by evaluating equation
            y = tf.cast(y, tf.float32) # tf.gather do not support first input.dtype=int32 on GPU
            cond = tf.logical_and(D_im <= margin + D_tn, tf.logical_not(tf.equal(
                                  tf.gather(y,tn_index[:,1]),tf.gather(y,im_index[:,1]))))
            full_idx = tf.cast(full_idx, tf.float32) # tf.gather do not support first input.dtype=int32 on GPU
            tup = tf.boolean_mask(full_idx, cond)
            tup = tf.cast(tup, tf.int32) # tf.gather do not support first input.dtype=int32 on GPU
            return tup
        
    def tf_LMNN_loss(self, X, y, tN, tup, mu, margin=None):
        ''' Calculates the LMNN loss (eq. 13 in paper)
        
        Arguments:
            X: N x ? matrix or tensor of data
            
            y: N x 1 vector, with class labels
            
            tN: (N*k) x 2 matrix, with targetmetric,  neighbour index
            
            tup: ? x 3, where M is the number of triplets that where found to
                 fullfill the imposter equation. First column in each row is the 
                 observation index, second column is the target neighbour index
                 and the third column is the imposter index
                 
            mu: scalar, weighting coefficient between the push and pull term
            
            margin: scalar, margin for the algorithm
        
        Output:
            loss: scalar, the LMNN loss
            D_pull: ? x 1 vector, with pull distance terms
            D_tN: ? x 1 vector, with the first push distance terms
            D_im: ? x 1 vector, with the second push distance terms
        '''
        with tf.name_scope('LMNN_loss'):
            margin = self.margin if margin is None else margin
            
            # Calculate distance
            D = self.metric_func(X, X) # N x N
            
            # Gather relevant distances
            D_pull = tf.gather_nd(D, tN)
            D_tn = tf.gather_nd(D, tup[:,:2])
            D_im = tf.gather_nd(D, tup[:,::2])
            
            # Calculate pull and push loss
            pull_loss = tf.reduce_sum(D_pull)
            push_loss = tf.reduce_sum(margin + D_tn - D_im)            
            
            # Total loss
            loss = (1-mu) * pull_loss + mu * push_loss
            return loss, D_pull, D_tn, D_im
        
    def fit(self,  Xtrain, ytrain, k, mu=0.5, maxEpoch=100, learning_rate=1e-4, 
              batch_size=50, val_set=None, run_id = None, snapshot=10):
        """ Function for training the LMNN algorithm
        
        Arguments:
            Xtrain: Tensor of data [N, ?]   
            ytrain: Vector of labels [N, ]                
            k: integer, number of target neighbours
            mu: float, in interval [0, 1]. Weighting between push and pull term
            maxEpoch: integer, maximum number of iterations to train
            learning_rate: float>0, learning rate for optimizer
            batch_size: integer, number of samples to evaluate in each step 
            val_set: tuple, with two elements with same format as Xtrain, ytrain
            run_id: str, name of the folder where results are stored
            snapshot: integer, determining how often the accuracy should be
                evaluated
        """
        
        # Tensorboard file writers
        run_id = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M') if run_id \
                 is None else run_id
        loc = self.dir_loc + '/' + run_id
        if not os.path.exists(self.dir_loc): os.makedirs(self.dir_loc)
        if self.verbose == 2: 
            self.train_writer = tf.summary.FileWriter(loc + '/train')
        
        # Check for validation set
        validation = False
        if val_set:
            validation = True
            Xval, yval = val_set
            if self.verbose == 2:
                self.val_writer = tf.summary.FileWriter(loc + '/val')
        
        # Training parameters
        Xtrain = Xtrain.astype('float32')
        ytrain = ytrain.astype('int32')
        N_train = Xtrain.shape[0]
        n_batch_train = int(np.ceil(N_train / batch_size))
        print(50*'-')
        print('Number of training samples:    ', N_train)
        if validation:
            Xval = Xval.astype('float32')
            yval = yval.astype('int32')
            N_val = Xval.shape[0]
            n_batch_val = int(np.ceil(N_val / batch_size))
            print('Number of validation samples:  ', N_val)
        print(50*'-')
        
        # Target neighbours
        tN = self.findTargetNeighbours(Xtrain, ytrain, k, name='Training')
        if validation:
            tN_val = self.findTargetNeighbours(Xval, yval, k, name='Validation')
        
        # Placeholders for data
        global_step = tf.Variable(0, trainable=False)
        Xp = tf.placeholder(tf.float32, shape=(None, *Xtrain.shape[1:]), name='In_features')
        yp = tf.placeholder(tf.int32, shape=(None,), name='In_targets')
        tNp = tf.placeholder(tf.int32, shape=(None, 2), name='In_targetNeighbours')
        
        # Imposters
        tup = self.tf_findImposters(Xp, yp, tNp)
        
        # Loss func and individual distance terms
        LMNN_loss, D_1, D_2, D_3 = self.tf_LMNN_loss(Xp, yp, tNp, tup, mu)
        
        # Optimizer
        optimizer = self.optimizer(learning_rate = learning_rate)
        trainer = optimizer.minimize(LMNN_loss, global_step=global_step)
        
        # Summaries
        n_tup = tf.shape(tup)[0]
        true_imp = tf.cast(tf.less(D_3, D_2), tf.float32)
        tf.summary.scalar('Loss', LMNN_loss) 
        tf.summary.scalar('Num_imp', n_tup)
        tf.summary.scalar('Loss_pull', tf.reduce_sum(D_1))
        tf.summary.scalar('Loss_push', tf.reduce_sum(self.margin + D_2 - D_3))
        tf.summary.histogram('Rel_push_dist', D_3 / (D_2 + self.margin))
        tf.summary.scalar('True_imp', tf.reduce_sum(true_imp))
        tf.summary.scalar('Frac_true_imp', tf.reduce_mean(true_imp))
        merged = tf.summary.merge_all()
        
        # Initilize
        init = tf.global_variables_initializer()
        self.session.run(init)
        if self.verbose==2: self.train_writer.add_graph(self.session.graph)
        
        # Training
        stats = stat_logger(maxEpoch, n_batch_train, verbose=self.verbose)
        stats.on_train_begin() # Start training
        for e in range(maxEpoch):
            stats.on_epoch_begin() # Start epoch
            
            # Permute target neighbours
            tN = np.random.permutation(tN)
            
            # Do backpropagation
            for b in range(n_batch_train):
                stats.on_batch_begin() # Start batch
                
                # Sample target neighbours and extract data from these
                tN_batch = tN[k*batch_size*b:k*batch_size*(b+1)]
                idx, inv_idx = np.unique(tN_batch, return_inverse=True)
                inv_idx = np.reshape(inv_idx, (-1, 2))
                X_batch = Xtrain[idx]
                y_batch = ytrain[idx]
                feed_data = {Xp: X_batch, yp: y_batch, tNp: inv_idx}
                
                # Evaluate graph
                _, loss_out, ntup_out, summ = self.session.run(
                    [trainer, LMNN_loss, n_tup, merged], 
                    feed_dict=feed_data)
                
                # Save stats
                stats.add_stat('loss', loss_out)
                stats.add_stat('#imp', ntup_out)                   
                
                # Save to tensorboard
                if self.verbose==2: 
                    self.train_writer.add_summary(summ, global_step=b+n_batch_train*e)
                stats.on_batch_end() # End batch
                
            # Evaluate accuracy every 'snapshot' epoch (expensive to do) and
            # on the last epoch
#            if e % snapshot == 0 or e == maxEpoch-1:
#                acc = self.evaluate(Xtrain, ytrain, Xtrain, ytrain, k=k, batch_size=batch_size)
#                stats.add_stat('acc', acc)
#                if self.verbose==2:
#                    summ = tf.Summary(value=[tf.Summary.Value(tag='Accuracy', simple_value=acc)])
#                    self.train_writer.add_summary(summ, global_step=b+n_batch_train*e)
            
            # Do validation if val_set is given and we are in the snapshot epoch
            # or at the last epoch
            if validation and (e % snapshot == 0 or e == maxEpoch-1):
                # Evaluate loss and tuples on val data
                tN_val = np.random.permutation(tN_val)
                for b in range(n_batch_val):
                    tN_batch = tN_val[k*batch_size*b:k*batch_size*(b+1)]
                    idx, inv_idx = np.unique(tN_batch, return_inverse=True)
                    inv_idx = np.reshape(inv_idx, (-1, 2))
                    X_batch = Xval[idx]
                    y_batch = yval[idx]
                    feed_data = {Xp: X_batch, yp: y_batch, tNp: inv_idx}
                    loss_out, ntup_out = self.session.run([LMNN_loss, n_tup], 
                                                          feed_dict=feed_data)
                    stats.add_stat('loss_val', loss_out)
                    stats.add_stat('#imp_val', ntup_out)
                
                # Compute accuracy
                acc = self.evaluate(Xval, yval, Xtrain, ytrain, k=k, batch_size=batch_size)
                stats.add_stat('acc_val', acc)
                
                if self.verbose==2:
                    # Write stats to summary protocol buffer
                    summ = tf.Summary(value=[
                        tf.Summary.Value(tag='Loss', simple_value=np.mean(stats.get_stat('loss_val'))),
                        tf.Summary.Value(tag='NumberOfImposters', simple_value=np.mean(stats.get_stat('#imp_val'))),
                        tf.Summary.Value(tag='Accuracy', simple_value=np.mean(stats.get_stat('acc_val')))])
             
                    # Save to tensorboard
                    self.val_writer.add_summary(summ, global_step=n_batch_train*e)
            
            stats.on_epoch_end() # End epoch
            
            # Check if we should terminate
            if stats.terminate: break
            
            # Write stats to console (if verbose=True)
            stats.write_stats()
            
        stats.on_train_end() # End training
        
        # Save variables and training stats
        self.save_weights(run_id + '/trained_metric')
        stats.save(loc + '/training_stats')
        return stats
    
    def transform(self, X, batch_size=64):
        ''' Transform the data in X
        Arguments:
            X: N x ?, matrix or tensor of data
            batch_size: scalar, number of samples to transform in parallel
        Output:
            X_trans: N x ?, matrix or tensor with the transformed data
        '''
        # Parameters for transformer
        N = X.shape[0]
        n_batch = int(np.ceil(N / batch_size))
        
        # Find the shape of the transformed data by transforming a single observation
        X_new = self.session.run(self.transformer(tf.cast(X[:1], tf.float32)))
        X_trans = np.zeros((N, *X_new.shape[1:]))
        
        # Graph
        X_trans_p = tf.placeholder(tf.float32, (None, *X.shape[1:]), 'Xtrans_placeholder')
        op = self.transformer(X_trans_p)
        
        # Transform data in batches
        for b in range(n_batch):
            X_batch = X[batch_size*b:batch_size*(b+1)]
            #X_batch_trans = self.transformer(tf.cast(X_batch, tf.float32))
            #X_trans[batch_size*b:batch_size*(b+1)] = self.session.run(X_batch_trans)
            X_trans[batch_size*b:batch_size*(b+1)] = self.session.run(
                    op, feed_dict={X_trans_p: X_batch})
        return X_trans
   
    def findTargetNeighbours(self, X, y, k, do_pca=True, name=''):
        ''' Numpy/sklearn implementation to find target neighbours for large 
            datasets. This function cannot use the GPU and thus runs on the CPU,
            but instead uses an advance ball-tree method.
        Arguments:
            X: N x ?, metrix or tensor with data
            y: N x 1, vector with labels
            k: scalar, number of target neighbours to find
            do_pca: bool, if true then the data will first be projected onto
                a pca-space which captures 95% of the variation in data
            name: str, name of the dataset
        Output:
            tN: (N*k) x 2 matrix, with target neighbour index. 
        '''
        print(50*'-')
        # Reshape data into 2D
        N = X.shape[0]
        X = np.reshape(X, (N, -1))
        if do_pca:
            print('Doing PCA')
            pca= PCA(n_components = 0.95)
            X = pca.fit_transform(X)
        val = np.unique(y) 
        counter = 1
        tN_count = 0
        tN = np.zeros((N*k, 2), np.int32)
        # Iterate over each class
        for c in val:
            progressBar(counter, len(val), 
                        name='Finding target neighbours for '+name)
            idx = np.where(y==c)[0]
            n_c = len(idx)
            x = X[idx]
            # Find the nearest neighbours
            nbrs = NearestNeighbors(n_neighbors=k+1, algorithm='brute')
            nbrs.fit(x)
            _, indices = nbrs.kneighbors(x)
            for kk in range(1,k+1):
                tN[tN_count:tN_count+n_c,0] = idx[indices[:,0]]
                tN[tN_count:tN_count+n_c,1] = idx[indices[:,kk]]
                tN_count += n_c
            counter += 1
        print('')
        print(50*'-')
        return tN
    
    def KNN_classifier(self, Xtest, Xtrain, ytrain, k, batch_size=50):
        ''' KNN classifier using sklearns library. This function runs the
            calculates on the CPU, so it is slow, but it can handle large amount
            of data.
            
        Arguments:
            Xtest: M x ? metrix or tensor with test data for which we want to
                   predict its classes for
            Xtrain: N x ? matrix or tensor with training data
            ytrain: N x 1 vector with class labels for the training data
            k: scalar, number of neighbours to look at
            batch_size: integer, number of samples to transform in parallel
        
        Output:
            pred: M x 1 vector with predicted class labels for the test set
        '''
        Ntest = Xtest.shape[0]
        Ntrain = Xtrain.shape[0]
        Xtest_t = self.transform(Xtest, batch_size=batch_size)
        Xtrain_t = self.transform(Xtrain, batch_size=batch_size)
        Xtest_t = np.reshape(Xtest_t, (Ntest, -1))
        Xtrain_t = np.reshape(Xtrain_t, (Ntrain, -1))
        same = np.array_equal(Xtest, Xtrain)
        if same: # if train and test is same, account for over estimation of
                 # performance by one more neighbour and zero weight to the first
            classifier = KNeighborsClassifier(n_neighbors = k+1, weights=weight_func, 
                                              algorithm='brute')
            classifier.fit(Xtrain_t, ytrain)
            pred = classifier.predict(Xtest_t)
        else:
            classifier = KNeighborsClassifier(n_neighbors = k, algorithm='brute')
            classifier.fit(Xtrain_t, ytrain)
            pred = classifier.predict(Xtest_t)
        return pred
    
    def evaluate(self, Xtest, Ytest, Xtrain, ytrain, k, batch_size=50):
        ''' Evaluates the current metric
        
        Arguments:
            Xtest: M x ? metrix or tensor with test data for which we want to
                   predict its classes for
            Xtrain: N x ? matrix or tensor with training data
            ytrain: N x 1 vector with class labels for the training data
            k: scalar, number of neighbours to look at
            batch_size: integer, number of samples to transform in parallel
        
        Output
            accuracy: scalar, accuracy of the prediction for the current metric
        '''
        pred = self.KNN_classifier(Xtest, Xtrain, ytrain, k, batch_size)
        accuracy = np.mean(pred == Ytest)
        return accuracy
    
    def save_weights(self, filename, step=None):
        ''' Save all weights/variables in the current session to a file 
        Arguments:
            filename: str, name of the file to write to
            step: integer, appended to the filename to distingues different saved
                files from each other
        '''
        saver = tf.train.Saver(tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES))
        saver.save(self.session, self.dir_loc+'/'+filename, global_step = step)
    
    def get_weights(self):
        weights = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES)
        return self.session.run(weights)
    
    
#%%
if __name__ == '__main__':
    pass