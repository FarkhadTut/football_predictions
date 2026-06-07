import type { ReactElement } from "react";

import { isCachedPrediction, type PredictResponse } from "../../api/client";
import { usePredictMutation } from "../../api/queries";

export interface ModelPanelProps {
  matchId: number;
}

function formatProb(p: number): string {
  return `${(p * 100).toFixed(1)}%`;
}

function Markets({ result }: { result: PredictResponse }): ReactElement {
  if (!isCachedPrediction(result)) {
    return (
      <p>
        Model fit enqueued (run #{result.model_run_id}); refresh to see results once it completes.
      </p>
    );
  }
  const marketNames = Object.keys(result.markets).sort();
  return (
    <div>
      <p>
        Model version: <code>{result.model_version}</code>
      </p>
      {marketNames.map((market) => {
        const outcomes = result.markets[market]!;
        const outcomeNames = Object.keys(outcomes).sort();
        return (
          <div key={market}>
            <h3>{market}</h3>
            <ul>
              {outcomeNames.map((outcome) => (
                <li key={outcome}>
                  {outcome}: {formatProb(outcomes[outcome]!)}
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </div>
  );
}

export function ModelPanel({ matchId }: ModelPanelProps): ReactElement {
  const mutation = usePredictMutation(matchId);

  return (
    <section aria-labelledby="model-panel-heading">
      <h2 id="model-panel-heading">Model</h2>
      <button
        type="button"
        onClick={() => mutation.mutate({ force_refit: false })}
        disabled={mutation.isPending}
      >
        {mutation.isPending ? "Running…" : "Run prediction"}
      </button>
      {mutation.isError && <p role="alert">Prediction failed: {mutation.error.message}</p>}
      {mutation.data && <Markets result={mutation.data} />}
      {!mutation.data && !mutation.isPending && !mutation.isError && (
        <p>Click "Run prediction" to fetch the latest model output.</p>
      )}
    </section>
  );
}
