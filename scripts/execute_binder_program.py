"""
Multiprocess executing binder programs.
"""

import time
import json
import argparse
from functools import cmp_to_key
import platform, multiprocessing
import os
import time

from nsql.nsql_exec import Executor, NeuralDB
from utils.normalizer import normalize_sql, postprosess_sql
from utils.utils import load_data_split, majority_vote
from utils.evaluator import Evaluator

ROOT_DIR = os.path.join(os.path.dirname(__file__), "../")


def worker_execute(
        pid,
        args,
        dataset,
        nsql_dict,
        keys
):
    """
    A worker process for execution.
    """
    result_dict = dict()
    n_total_samples, n_correct_samples = 0, 0
    for eid, data_item in enumerate(dataset):
        eid = str(eid)
        if eid not in nsql_dict:
            continue
        print(f"Process#{pid}: eid {eid}, wtq-id {data_item['id']}")
        result_dict[eid] = dict()
        result_dict[eid]['question'] = data_item['question']
        result_dict[eid]['gold_answer'] = data_item['answer_text']
        n_total_samples += 1
        table = data_item['table']
        title = table['page_title']
        executor = Executor(args, keys)
        # Execute
        exec_answer_list = []
        nsql_exec_answer_dict = dict()
        for idx, (nsql, logprob) in enumerate(nsql_dict[eid]['nsqls']):
            print(f"Process#{pid}: eid {eid}, original_id {data_item['id']}, executing program#{idx}, logprob={logprob}")
            try:
                if nsql in nsql_exec_answer_dict:
                    exec_answer = nsql_exec_answer_dict[nsql]
                else:
                    db = NeuralDB(
                        tables=[{"title": title, "table": table}]
                    )
                    # TODO: Unify these two as one postprocess?
                    nsql = normalize_sql(
                        sql_str=nsql,
                        all_headers=db.get_table_df().columns.to_list(),
                        table_title=title
                    )
                    try:
                        nsql = postprosess_sql(
                            sql_str=nsql,
                            df=db.get_table_df(),
                        )
                    except:
                        pass
                    exec_answer = executor.nsql_exec(nsql, db, verbose=args.verbose)
                    if isinstance(exec_answer, str):
                        exec_answer = [exec_answer]
                    nsql_exec_answer_dict[nsql] = exec_answer
                exec_answer_list.append(exec_answer)
            except Exception as e:
                print(f"Process#{pid}: Execution error {e}")
                exec_answer = '<error>'
                exec_answer_list.append(exec_answer)
            # Store tmp execution answers
            if nsql_dict[eid].get('exec_answers', None) is None:
                nsql_dict[eid]['exec_answers'] = []
            nsql_dict[eid]['exec_answers'].append(exec_answer)
        # Majority vote to determine the final prediction answer
        pred_answer, pred_answer_nsqls = majority_vote(
            nsqls=nsql_dict[eid]['nsqls'],
            pred_answer_list=exec_answer_list,
            allow_none_and_empty_answer=args.allow_none_and_empty_answer,
            answer_placeholder=args.answer_placeholder,
            vote_method=args.vote_method,
            answer_biased=args.answer_biased,
            answer_biased_weight=args.answer_biased_weight
        )
        # Evaluate
        result_dict[eid]['pred_answer'] = pred_answer
        result_dict[eid]['nsql'] = pred_answer_nsqls
        gold_answer = data_item['answer_text']
        score = Evaluator().evaluate(
            pred_answer,
            gold_answer,
            dataset=args.dataset,
            question=result_dict[eid]['question']
        )
        n_correct_samples += score
        print(f'Process#{pid}: pred answer: {pred_answer}')
        print(f'Process#{pid}: gold answer: {gold_answer}')
        if score == 1:
            print(f'Process#{pid}: Correct!')
        else:
            print(f'Process#{pid}: Wrong.')
        print(f'Process#{pid}: Accuracy: {n_correct_samples}/{n_total_samples}')

    # Save tmp execution answers
    # TODO: whether to save the temp results in released version
    with open(os.path.join(args.save_dir, f'{args.output_program_execution_file[:-5]}_tmp_{pid}.json'), 'w') as f:
        json.dump(nsql_dict, f, indent=4)

    return result_dict


def main():
    # Build paths
    args.api_keys_file = os.path.join(ROOT_DIR, args.api_keys_file)
    args.save_dir = os.path.join(ROOT_DIR, args.save_dir)
    os.makedirs(args.save_dir, exist_ok=True)

    # Load dataset
    start_time = time.time()
    dataset = load_data_split(args.dataset, args.dataset_split)

    # Load openai keys
    with open(args.api_keys_file, 'r') as f:
        keys = [line.strip() for line in f.readlines()]

    # Load programs and process as a unified format
    with open(os.path.join(args.save_dir, args.input_program_file), 'r') as f:
        data = json.load(f)
    nsql_dict = dict()
    for eid, data_dict in data.items():
        if data[eid]['generations']:
            nsqls = data[eid]['generations']
        else:
            nsqls = [['<dummy program>', 0.]]
        nsql_dict[eid] = {'nsqls': nsqls}

    # Split by processes
    nsql_dict_group = [dict() for _ in range(args.n_processes)]
    for idx, eid in enumerate(nsql_dict.keys()):
        nsql_dict_group[idx % args.n_processes][eid] = nsql_dict[eid]

    # Execute programs
    result_dict = dict()
    worker_results = []
    pool = multiprocessing.Pool(processes=args.n_processes)
    for pid in range(args.n_processes):
        worker_results.append(pool.apply_async(worker_execute, args=(
            pid,
            args,
            dataset,
            nsql_dict_group[pid],
            keys
        )))

    # Merge worker results
    for r in worker_results:
        worker_result_dict = r.get()
        result_dict.update(worker_result_dict)
    pool.close()
    pool.join()
    n_correct_samples = 0
    for eid, item in result_dict.items():
        pred_answer, gold_answer = item['pred_answer'], item['gold_answer']
        n_correct_samples += Evaluator().evaluate(
            pred_answer,
            gold_answer,
            dataset=args.dataset,
            question=result_dict[eid]['question']
        )
    print(f'Overall Accuracy: {n_correct_samples}/{len(result_dict)}')

    # Save program executions
    with open(os.path.join(args.save_dir, args.output_program_execution_file), 'w') as f:
        json.dump(result_dict, f)

    print(f'Done. Elapsed time: {time.time() - start_time}')


if __name__ == '__main__':
    if platform.system() == "Darwin":
        multiprocessing.set_start_method('spawn')

    parser = argparse.ArgumentParser()

    # File path or name
    parser.add_argument('--dataset', type=str, default='tab_fact',
                        choices=['wikitq', 'tab_fact', 'mmqa'])
    parser.add_argument('--dataset_split', type=str, default='validation', choices=['train', 'validation', 'test'])
    parser.add_argument('--api_keys_file', type=str, default='key.txt')
    parser.add_argument('--save_dir', type=str, default='results/')
    parser.add_argument('--qa_retrieve_pool_file', type=str, default='templates/prompts/qa_retrieve_pool.txt')
    parser.add_argument('--input_program_file', type=str,
                        default='binder_program_tab_fact_validation.json')
    parser.add_argument('--output_program_execution_file', type=str,
                        default='binder_program_execution_tab_fact_validation.json')

    # Multiprocess options
    parser.add_argument('--n_processes', type=str, default=4)

    # Execution options
    parser.add_argument('--use_majority_vote', action='store_false',
                        help='Whether use majority vote to determine the prediction answer.')
    parser.add_argument('--allow_none_and_empty_answer', action='store_true',
                        help='Whether regarding none and empty executions as a valid answer.')
    parser.add_argument('--allow_error_answer', action='store_true',
                        help='Whether regarding error execution as a valid answer.')
    parser.add_argument('--answer_placeholder', type=str, default='<error|empty>',
                        help='Placeholder answer if execution error occurs.')
    parser.add_argument('--vote_method', type=str, default='simple',
                        choices=['simple', 'prob', 'answer_biased'])
    parser.add_argument('--answer_biased', type=int, default=None,
                        help='The answer to be biased w. answer_biased_weight in majority vote.')
    parser.add_argument('--answer_biased_weight', type=float, default=None,
                        help='The weight of the answer to be biased in majority vote.')

    # Debugging options
    parser.add_argument('--verbose', action='store_true')

    args = parser.parse_args()
    print("Args info:")
    for k in args.__dict__:
        print(k + ": " + str(args.__dict__[k]))

    main()
