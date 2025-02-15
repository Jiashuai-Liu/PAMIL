import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
# from models.model_sqmil import SQMIL_NIC
from models.model_protoattention import PAMIL


import pdb
import os
import pandas as pd
from utils.utils import *
from utils.core_utils import Accuracy_Logger
from sklearn.metrics import roc_auc_score, roc_curve, auc, accuracy_score, classification_report
from sklearn.metrics import auc as calc_auc
from sklearn.preprocessing import label_binarize
import matplotlib.pyplot as plt

def initiate_model(args, ckpt_path, cur):
    device=torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print('Init Model')    
    
    # define the model for PAMIL
    model_dict = {"dropout": args.drop_out, 'n_classes': args.n_classes}
    
    if args.model_size is not None and args.model_type != 'mil':
        model_dict.update({"size_arg": args.model_size})
    
    if args.model_type in ['PAMIL']:
        model_dict.update({'n_protos': args.num_protos})
        model_dict.update({'proto_path': os.path.join(args.proto_path, f'{args.task}_{args.num_protos}_{cur}', f'train_instance_feats_proto.npy')})
        model_dict.update({'proto_pred': args.proto_pred})
        model_dict.update({'proto_weight': args.proto_weight})
        model_dict.update({'inst_pred': args.inst_pred})
        model_dict.update({'k_sample': args.k_sample})
        model = PAMIL(**model_dict)
    else:
        raise NotImplementedError
    
    print_network(model)

    ckpt = torch.load(ckpt_path)
    ckpt_clean = {}
    for key in ckpt.keys():
        if 'instance_loss_fn' in key:
            continue
        ckpt_clean.update({key.replace('.module', ''):ckpt[key]})
    model.load_state_dict(ckpt_clean, strict=True)

    model.to(device)
    model.eval()
    return model

def eval_(dataset, args, ckpt_path, cur):
    print(ckpt_path)
    model = initiate_model(args, ckpt_path, cur)
    
    print('Init Loaders')
    loader = get_simple_loader(dataset)
    patient_results, test_error, auc, df, _, df_inst = summary(model, loader, args)
    print('test_error: ', test_error)
    print('auc: ', auc)
    return model, patient_results, test_error, auc, df, df_inst

def summary(model, loader, args):
    n_classes = args.n_classes
    model_type = args.model_type
    
    device=torch.device("cuda" if torch.cuda.is_available() else "cpu")
    acc_logger = Accuracy_Logger(n_classes=n_classes)
    model.eval()
    test_loss = 0.
    test_error = 0.
    all_inst_label = []
    all_slide_ids = []
    all_inst_score = []
    all_inst_pred = []
    
    all_inst_score_pos = []
    all_inst_score_neg = []
    
    inst_probs = [[] for _ in range(n_classes+1)]
    inst_preds = [[] for _ in range(n_classes+1)]
    inst_binary_labels = [[] for _ in range(n_classes+1)]
    
    pos_accs_each_wsi = [[] for _ in range(n_classes)]
    neg_accs_each_wsi = []

    all_probs = np.zeros((len(loader), n_classes))
    all_labels = np.zeros((len(loader), n_classes))
    all_preds = np.zeros((len(loader), n_classes))

    slide_ids = loader.dataset.slide_data['slide_id']
    patient_results = {}
        
    for batch_idx, (data, label, cors, inst_label, slide_id) in enumerate(loader):
        data, label = data.to(device), label.to(device)
        slide_id = slide_id[0]
        index_label = torch.nonzero(label.squeeze(0)).to(device)
        logits, Y_prob, Y_hat, instance_dict = model(data, inst_pred=args.inst_pred, label=label)
        Y_prob = Y_prob.squeeze(0)
        # inst_label = inst_label[0]
        score = instance_dict['A_raw'].T
        
        # inst-level evaluation
        if inst_label != []:
            all_slide_ids += [os.path.join(slide_id, f"{cor}.png") for cor in cors]
            all_inst_label += inst_label
            inst_score = score.detach().cpu().numpy()

            if args.task == 'camelyon':
                label_hot = np.array([[label, 1 - label] for label in inst_label])
            else:
                label_hot = label_binarize(inst_label, classes=[i for i in range(n_classes+1)])
            inst_probs_tmp = [] 
            inst_preds_tmp = []
            
            for class_idx in range(n_classes):
                if class_idx not in index_label:
                    inst_score[:, class_idx] = [-1] * len(inst_label)
                    continue
                
                inst_score_one_class = inst_score[:, class_idx]
                # if len(set(inst_score_one_class))>1:
                inst_score_one_class = list((inst_score_one_class-inst_score_one_class.min())/
                                            max(inst_score_one_class.max()-inst_score_one_class.min(),1e-10))
                inst_score_one_class = list(inst_score_one_class)
                inst_score[:, class_idx] = inst_score_one_class

                inst_probs[class_idx] += inst_score_one_class
                inst_probs_tmp.append(inst_score_one_class)

                inst_preds[class_idx]+=[0 if i<0.5 else 1 for i in inst_score_one_class]
                inst_preds_tmp.append([0 if i<0.5 else 1 for i in inst_score_one_class])

                inst_binary_labels[class_idx]+=list(label_hot[:,class_idx])
                
            if inst_preds_tmp:
                inst_preds_tmp = np.mean(np.stack(inst_preds_tmp), axis=0)
            else:
                inst_preds_tmp = [0]*len(inst_label)
            inst_preds_tmp = [1 if i==0 else 0 for i in inst_preds_tmp]
            inst_preds[n_classes] += inst_preds_tmp
            inst_binary_labels[n_classes] += list(label_hot[:,n_classes])
            
            if inst_probs_tmp:
                neg_score = np.mean(np.stack(inst_probs_tmp), axis=0) #三类平均，越低越是neg
                # if len(set(neg_score))>1:
                neg_score = list((neg_score-neg_score.min())/max(neg_score.max()-neg_score.min(),1e-10))
                # neg_score = list(neg_score)
            else:
                neg_score = [0]*len(inst_label)
            
            all_inst_score.append(inst_score)
            all_inst_score_neg += neg_score
            
        # wsi-level evaluation
        acc_logger.log(Y_hat, label)
        probs = Y_prob.cpu().detach().numpy()
        Y_hat_one_hot = np.zeros(n_classes)
        Y_hat_one_hot[Y_hat.cpu().numpy()]=1

        all_probs[batch_idx] = probs
        all_labels[batch_idx] = label.squeeze().detach().cpu().numpy()
        all_preds[batch_idx] = Y_hat_one_hot
        
        patient_results.update({slide_id: {'slide_id': np.array(slide_id), 'prob': probs, 
                                           'label': torch.nonzero(label.squeeze()).squeeze().cpu().numpy()}})
        error = calculate_error(Y_hat, label)
        test_error += error
    del data
    test_error /= len(loader)

    # calculate inst_auc and inst_acc
    
    all_inst_score = np.concatenate(all_inst_score, axis=0)
    all_normal_score = [1-x for x in all_inst_score_neg] #转化为是normal类的概率 越高越好
    
    # get inst_pred inst_acc
    inst_accs = []
    for i in range(n_classes+1):
        if len(inst_binary_labels[i])==0:
            continue
        inst_accs.append(accuracy_score(inst_binary_labels[i], inst_preds[i]))
        inst_acc = np.mean(inst_accs)
        print('class {}'.format(str(i)))
        print(classification_report(inst_binary_labels[i], inst_preds[i],zero_division=1))
    
    # get inst_auc
    inst_aucs = []
    for class_idx in range(n_classes):
        inst_score_sub = inst_probs[class_idx]
        if len(inst_score_sub)==0:
            continue
        fpr, tpr, _ = roc_curve(inst_binary_labels[class_idx], inst_score_sub)
        inst_aucs.append(calc_auc(fpr, tpr))
    fpr,tpr,_ = roc_curve(inst_binary_labels[n_classes], all_normal_score)
    inst_aucs.append(calc_auc(fpr, tpr))
    inst_auc = np.nanmean(np.array(inst_aucs))


    aucs = []
    binary_labels = all_labels
    for class_idx in range(n_classes):
        fpr, tpr, _ = roc_curve(binary_labels[:, class_idx], all_probs[:, class_idx])
        aucs.append(calc_auc(fpr, tpr))

    auc_score = np.nanmean(np.array(aucs))

    results_dict = {'slide_id': slide_ids}
    for c in range(args.n_classes):
        results_dict.update({'label_{}'.format(c): all_labels[:,c]})
        results_dict.update({'prob_{}'.format(c): all_probs[:,c]})
        results_dict.update({'pred_{}'.format(c): all_preds[:,c]})
        
    df = pd.DataFrame(results_dict)
    all_inst_score = np.insert(all_inst_score, args.n_classes, values=all_normal_score, axis=1)
    inst_results_dict = {'filename':all_slide_ids,'label':all_inst_label}
    for c in range(args.n_classes+1):
        inst_results_dict.update({'prob_{}'.format(c): all_inst_score[:,c]})
        
    df_inst = pd.DataFrame(inst_results_dict)
    
    return patient_results, test_error, [auc_score, inst_auc, inst_acc], df, acc_logger, df_inst
